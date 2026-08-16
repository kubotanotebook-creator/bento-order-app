"""
Parses リリテイ's monthly "日替わり弁当" PDF menu into per-date dish names for
the 3 fixed categories (fry / nofry / veg).

This is calibrated against the actual monthly PDF template リリテイ publishes
(a single-page calendar grid: weekday header, then repeating blocks of a
day-number row followed by 2 description lines per category per weekday).
Column positions are detected dynamically from each file rather than
hardcoded, so small month-to-month shifts in margins/layout should still
work. A real layout change (e.g. a different number of description lines,
or additional categories) could still confuse the parser — that's exactly
why the admin UI built on top of this always shows the extracted result for
review/editing before anything is saved to the live menu; never trust this
output blindly.
"""
import re
import unicodedata
from collections import defaultdict

import pdfplumber


def chunk_row(band_chars, gap_threshold=1.6):
    """Group characters on the same text line into words, splitting wherever
    there's a horizontal gap bigger than gap_threshold points."""
    chunks = []
    cur = []
    prev_x1 = None
    for c in sorted(band_chars, key=lambda c: c['x0']):
        if prev_x1 is not None and c['x0'] - prev_x1 > gap_threshold:
            chunks.append(cur)
            cur = []
        cur.append(c)
        prev_x1 = c['x1']
    if cur:
        chunks.append(cur)
    return chunks


def cluster_rows(chars, tol=1.5):
    """Group characters into text-line "bands" by their vertical position."""
    rows = defaultdict(list)
    for c in chars:
        rows[round(c['top'], 1)].append(c)
    tops = sorted(rows.keys())
    merged = []
    for t in tops:
        if merged and t - merged[-1][-1] <= tol:
            merged[-1].append(t)
        else:
            merged.append([t])
    bands = []
    for band in merged:
        band_chars = []
        for t in band:
            band_chars.extend(rows[t])
        bands.append((band[0], band_chars))
    return bands


def detect_column_anchors(bands):
    """Cluster the left-edge x0 of every text chunk (skipping pure-digit date
    chunks, which are centered rather than left-aligned) into 5 groups — one
    per weekday column — and return each group's leftmost x0 as its anchor."""
    starts = []
    for _top, band_chars in bands:
        for chunk in chunk_row(band_chars):
            text = ''.join(c['text'] for c in chunk)
            if re.fullmatch(r'[0-9]+', text):
                continue
            starts.append(chunk[0]['x0'])
    starts.sort()
    if len(starts) < 5:
        return None
    gaps = [(starts[i + 1] - starts[i], i) for i in range(len(starts) - 1)]
    gaps.sort(reverse=True)
    cut_points = sorted(i for _, i in gaps[:4])
    groups, prev = [], 0
    for cp in cut_points:
        groups.append(starts[prev:cp + 1])
        prev = cp + 1
    groups.append(starts[prev:])
    if len(groups) != 5:
        return None
    return [min(g) for g in groups]


def nearest_col(x0, anchors):
    diffs = [abs(x0 - a) for a in anchors]
    return diffs.index(min(diffs))


def split_merged_chunk(chunk, primary_col, anchors):
    """The source PDF occasionally glues two adjacent columns' text together
    with zero gap (no space character between them at all). Detect this by
    looking for a character landing almost exactly on a later column's
    anchor and split there."""
    pieces = []
    start = 0
    for idx in range(1, len(chunk)):
        x0 = chunk[idx]['x0']
        for col_idx, anchor in enumerate(anchors):
            if col_idx > primary_col and abs(x0 - anchor) < 1.2:
                pieces.append((primary_col, chunk[start:idx]))
                start = idx
                primary_col = col_idx
                break
    pieces.append((primary_col, chunk[start:]))
    return pieces


def row_to_columns(band_chars, anchors):
    result = defaultdict(str)
    for chunk in chunk_row(band_chars):
        primary_col = nearest_col(chunk[0]['x0'], anchors)
        for col_idx, sub_chunk in split_merged_chunk(chunk, primary_col, anchors):
            result[col_idx] += ''.join(c['text'] for c in sub_chunk)
    return [result.get(i, '') for i in range(5)]


DATE_RE_MD = re.compile(r'^(\d{1,2})月(\d{1,2})日$')
DATE_RE_D = re.compile(r'^(\d{1,2})$')


def parse_date_cell(text, cur_year, cur_month):
    text = text.strip()
    if not text:
        return None
    m = DATE_RE_MD.match(text)
    if m:
        return cur_year, int(m.group(1)), int(m.group(2))
    m = DATE_RE_D.match(text)
    if m:
        return cur_year, cur_month, int(m.group(1))
    return None


HALFWIDTH_KATA_RE = re.compile(r'[｡-ﾟ]+')


def fix_halfwidth_katakana(s):
    """The source PDF embeds a few dish names in half-width katakana glyphs
    (a font quirk); convert those back to normal full-width katakana."""
    return HALFWIDTH_KATA_RE.sub(lambda m: unicodedata.normalize('NFKC', m.group(0)), s)


def parse_menu_pdf(path_or_stream):
    """Returns (result, error). result is {date_str: {fry, nofry, veg}} for
    every weekday found; holiday/closed weeks are silently skipped (no menu
    that day). error is a Japanese user-facing message, or None on success."""
    try:
        with pdfplumber.open(path_or_stream) as pdf:
            page = pdf.pages[0]
            top_words = page.extract_words()
            chars = [c for c in page.chars if c['top'] > 320 and c['x0'] >= 95]
    except Exception:
        return {}, "PDFを開けませんでした。ファイルが壊れているか、対応していない形式の可能性があります。"

    bands = cluster_rows(chars)
    anchors = detect_column_anchors(bands)
    if not anchors:
        return {}, "PDFの表構造を認識できませんでした。手動でメニューを入力してください。"

    # Title area (top of page) has the year ("2026") and month ("8") as
    # separate word objects; find them by position rather than trusting
    # extract_text()'s line-grouping, which can scramble overlapping title text.
    header_words = [w for w in top_words if w['top'] < 150]
    year_candidates = [w['text'] for w in header_words if re.fullmatch(r'20\d{2}', w['text'])]
    month_candidates = [w['text'] for w in header_words if re.fullmatch(r'\d{1,2}', w['text'])]
    base_year = int(year_candidates[0]) if year_candidates else None
    base_month = int(month_candidates[0]) if month_candidates else None
    if not base_year or not base_month:
        return {}, "PDFから年月を読み取れませんでした。手動でメニューを入力してください。"

    weekday_chars = {'月', '火', '水', '木', '金'}
    rows_data = []
    for _top, band_chars in bands:
        cols = row_to_columns(band_chars, anchors)
        text_all = ''.join(cols)
        if set(text_all) and set(text_all) <= weekday_chars:
            continue  # the "月 火 水 木 金" weekday-label header row
        rows_data.append(cols)

    CONTENT_LABELS = ['fry1', 'fry2', 'nofry1', 'nofry2', 'veg1', 'veg2']
    weeks = []
    i = 0
    while i < len(rows_data):
        cols = rows_data[i]
        parsed_dates = [parse_date_cell(c, base_year, base_month) for c in cols]
        is_date_row = all(d is not None for d in parsed_dates) and all(c.strip() for c in cols)
        if is_date_row:
            week_entry = {'dates': parsed_dates, 'rows': {}}
            i += 1
            collected = []
            while i < len(rows_data):
                c2 = rows_data[i]
                pd2 = [parse_date_cell(c, base_year, base_month) for c in c2]
                if all(d is not None for d in pd2) and all(c.strip() for c in c2):
                    break
                collected.append(c2)
                i += 1
                if len(collected) == 6:
                    break
            if len(collected) == 6:
                for label, cols6 in zip(CONTENT_LABELS, collected):
                    week_entry['rows'][label] = cols6
            else:
                week_entry['closed'] = True
            weeks.append(week_entry)
        else:
            i += 1

    result = {}
    for w in weeks:
        for day_idx, (y, mo, d) in enumerate(w['dates']):
            date_str = f"{y}-{mo:02d}-{d:02d}"
            if w.get('closed'):
                continue
            entry = {}
            for cat, keys in [('fry', ('fry1', 'fry2')), ('nofry', ('nofry1', 'nofry2')), ('veg', ('veg1', 'veg2'))]:
                items = [fix_halfwidth_katakana(w['rows'][k][day_idx].strip()) for k in keys if w['rows'][k][day_idx].strip()]
                entry[cat] = ' / '.join(items)
            result[date_str] = entry

    if not result:
        return {}, "PDFからメニューを1件も抽出できませんでした。手動でメニューを入力してください。"
    return result, None
