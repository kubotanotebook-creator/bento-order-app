import os
import sys
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, g, request, session, redirect, url_for, render_template, flash, jsonify

from menu_pdf_parser import parse_menu_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Overridable so production deploys can point this at a persistent disk mount
# (the app's own directory is wiped on every redeploy on most PaaS hosts).
DB_PATH = os.environ.get("BENTO_DB_PATH", os.path.join(BASE_DIR, "bento.db"))

app = Flask(__name__)
DEFAULT_SECRET_KEY = "dev-secret-change-me"
DEFAULT_ADMIN_PASSWORD = "admin123"
app.secret_key = os.environ.get("BENTO_SECRET_KEY", DEFAULT_SECRET_KEY)
ADMIN_PASSWORD = os.environ.get("BENTO_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB upload cap

# Fail-fast against shipping the built-in default secret key / admin password.
# A known secret key lets anyone forge an "is_admin" session cookie, and the
# default admin password is public in this repo — either one in production is a
# real hole. We treat "$PORT is set" as the production signal (that's how the
# Procfile binds gunicorn on Cloud Run / Render / Heroku); local `python app.py`
# has no $PORT, so it only warns and still runs. Set BENTO_ALLOW_DEFAULT_SECRETS=1
# to override intentionally (e.g. a throwaway internal demo).
_using_default_secrets = (
    app.secret_key == DEFAULT_SECRET_KEY or ADMIN_PASSWORD == DEFAULT_ADMIN_PASSWORD
)
if _using_default_secrets:
    _warning = (
        "BENTO_SECRET_KEY / BENTO_ADMIN_PASSWORD がデフォルトのままです。"
        "本番では必ず環境変数で設定してください。"
    )
    _in_production = bool(os.environ.get("PORT"))
    _allow_defaults = os.environ.get("BENTO_ALLOW_DEFAULT_SECRETS") == "1"
    if _in_production and not _allow_defaults:
        raise RuntimeError(
            "本番起動を中止しました（デフォルトの秘密鍵/管理者パスワードのままです）。"
            + _warning
            + " どうしても既定値で起動する場合のみ BENTO_ALLOW_DEFAULT_SECRETS=1 を設定してください。"
        )
    print("WARNING: " + _warning, file=sys.stderr)

# All date/time logic is pinned to JST regardless of the server's own timezone.
# PaaS hosts (Cloud Run, Render, ...) run in UTC, which would otherwise shift
# every deadline by 9 hours. We work in *naive* datetimes that represent JST
# wall-clock time, so they stay directly comparable with the naive datetimes
# stored in the DB (deadlines, created_at).
JST = ZoneInfo("Asia/Tokyo")


def now_jst():
    """Current JST wall-clock time as a naive datetime."""
    return datetime.now(JST).replace(tzinfo=None)


def today_jst():
    """Today's date in JST."""
    return datetime.now(JST).date()

# 3 fixed categories, matching the noticeboard sheet (フライあり/フライなし/野菜).
# "フライ" is kept on fry/nofry because "あり"/"なし" alone reads as an
# order-yes/no state (there's a separate "注文なし" = no-order state shown in
# the same views), which was genuinely ambiguous. "やさい" alone is fine
# since nothing else in the UI could be confused with it.
CATEGORY_CODES = ["fry", "nofry", "veg"]
CATEGORY_LABELS = {"fry": "フライあり", "nofry": "フライなし", "veg": "やさい"}
CATEGORY_SHORT = {"fry": "フライ有り", "nofry": "なし", "veg": "野菜"}
# Even shorter than CATEGORY_SHORT, for squeezing a per-day count badge
# (e.g. "フライ2") into the narrow weekly print sheet's day-column header
# without the label pushing the number outside the colored badge.
CATEGORY_PRINT_SHORT = {"fry": "フライ", "nofry": "なし", "veg": "野菜"}
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]
# Same-day self-service cancel requests are only accepted before this time —
# after it, the admin's morning attendance-check process has usually already
# started, so further changes need to go through 松浦さん/陽介さん directly.
SAME_DAY_CANCEL_CUTOFF = time(9, 0)


@app.template_filter("weekday_jp")
def weekday_jp_filter(date_str):
    """Template filter: 'YYYY-MM-DD' -> '月'/'火'/... for display like 8/24(月)."""
    return WEEKDAY_JP[date.fromisoformat(date_str).weekday()]


# ---------- DB helpers ----------

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_date TEXT NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(item_date, category)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_date TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            menu_item_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'ordered',
            paid INTEGER NOT NULL DEFAULT 0,
            unit_price INTEGER NOT NULL DEFAULT 300,
            created_by TEXT NOT NULL DEFAULT 'self',
            created_at TEXT NOT NULL,
            FOREIGN KEY (menu_item_id) REFERENCES menu_items (id)
        );

        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            price INTEGER NOT NULL DEFAULT 300,
            cutoff_weekday INTEGER NOT NULL DEFAULT 4,
            cutoff_time TEXT NOT NULL DEFAULT '15:00'
        );

        INSERT OR IGNORE INTO settings (id, price, cutoff_weekday, cutoff_time)
        VALUES (1, 300, 4, '15:00');

        -- Per-week deadline overrides. The "settings" cutoff_weekday/cutoff_time
        -- gives an automatic default (previous week's same weekday/time), but an
        -- admin can pin a specific date/time for a given week (e.g. around a
        -- holiday) when registering that week's menu. If a week has no row here,
        -- the automatic default applies.
        CREATE TABLE IF NOT EXISTS week_deadlines (
            monday TEXT PRIMARY KEY,
            deadline_at TEXT NOT NULL
        );

        -- Lightweight "is this really you" check for the name-only employee
        -- login: birthday (MMDD, no year) is recorded the first time a name
        -- logs in, then must match on every later login from a new browser.
        -- Not real authentication, just enough to stop someone casually
        -- typing a coworker's name.
        CREATE TABLE IF NOT EXISTS employees (
            name TEXT PRIMARY KEY,
            birthday TEXT NOT NULL
        );

        -- Digitizes the paper チケット受け渡し表: one row per time
        -- 松浦さん/陽介さん physically hand someone a fresh 10-ticket booklet.
        -- This is what "残り枚数" and "天引き予定額" are computed from — the
        -- app has no way to observe real ticket handouts on its own, so this
        -- table only knows what's been logged here.
        CREATE TABLE IF NOT EXISTS ticket_issuances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_name TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            quantity INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL
        );

        -- Days リリテイ doesn't deliver (定休日 / 祝日). The monthly PDF writes
        -- the reason into the menu cells for those days, which would otherwise
        -- be imported as if it were a dish name and offered as an orderable
        -- choice ("フライあり: 定休日"). A date listed here is shown as closed
        -- and cannot be ordered.
        CREATE TABLE IF NOT EXISTS closed_days (
            item_date TEXT PRIMARY KEY,
            reason TEXT NOT NULL DEFAULT '定休日'
        );
        """
    )
    db.commit()

    # Enforce "one active order per person per day" at the DB level, closing the
    # check-then-insert race a double-submit could otherwise slip through to
    # create two counted bento. Partial index: only 'ordered' rows are
    # constrained, so any number of 'cancelled' rows for the same day are fine.
    try:
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_orders_active "
            "ON orders(employee_name, order_date) WHERE status = 'ordered'"
        )
        db.commit()
    except sqlite3.IntegrityError:
        # Legacy data already contains duplicate active orders, so the index
        # can't be built yet. Start anyway (unprotected) but make it loud so an
        # admin can resolve the duplicates, after which a restart adds the index.
        print(
            "WARNING: 既存データに同一人物・同一日の重複注文があるため、"
            "重複防止インデックスを作成できませんでした。管理画面で重複を解消してください。",
            file=sys.stderr,
        )

    # Employee-requested cancellations (for orders past that week's deadline)
    # don't cancel immediately — the admin approves them, since the weekly
    # paper checklist is already printed by then and only the admin can keep
    # it in sync. NULL = no pending request.
    try:
        db.execute("ALTER TABLE orders ADD COLUMN cancel_requested_at TEXT")
        db.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    db.close()


def get_settings():
    db = get_db()
    row = db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return row


# ---------- Closed days (定休日) ----------

# Words リリテイ writes into a menu cell to mean "no delivery this day". Holiday
# names (敬老の日 etc.) are deliberately NOT listed: they appear alongside these
# markers on the same date, and a name alone is too easy to confuse with a real
# dish. One marker anywhere in the day's cells closes the whole day.
CLOSED_DAY_MARKERS = ("定休日", "休業", "お休み", "弁当なし", "配達なし")
DEFAULT_CLOSED_REASON = "定休日"


def looks_like_closed_day(names):
    """True if any of this date's parsed cell texts marks it as a closed day."""
    for name in names:
        if name and any(marker in name for marker in CLOSED_DAY_MARKERS):
            return True
    return False


def get_closed_days(db, start=None, end=None):
    """{date_str: reason} for closed days, optionally limited to a date range."""
    if start and end:
        rows = db.execute(
            "SELECT * FROM closed_days WHERE item_date >= ? AND item_date <= ?",
            (start, end),
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM closed_days").fetchall()
    return {r["item_date"]: r["reason"] for r in rows}


def set_day_closed(db, item_date, reason=DEFAULT_CLOSED_REASON):
    """Mark a date closed and drop the junk menu items the PDF import created
    for it. Menu items already referenced by an order are left alone (the
    foreign key forbids deleting them); the day still reads as closed."""
    db.execute(
        "INSERT INTO closed_days (item_date, reason) VALUES (?, ?) "
        "ON CONFLICT(item_date) DO UPDATE SET reason = excluded.reason",
        (item_date, reason or DEFAULT_CLOSED_REASON),
    )
    db.execute(
        "DELETE FROM menu_items WHERE item_date = ? AND id NOT IN "
        "(SELECT menu_item_id FROM orders)",
        (item_date,),
    )


def set_day_open(db, item_date):
    db.execute("DELETE FROM closed_days WHERE item_date = ?", (item_date,))


# ---------- Admin attention (未対応の件数) ----------

def pending_cancel_requests(db):
    """Employee cancel requests still waiting on the admin, oldest first.

    Deliberately not limited to today or the future: an unanswered request for
    a date that has already passed is the worst case (the bento was delivered
    and charged anyway), so it has to keep showing until someone acts on it.
    """
    rows = db.execute(
        "SELECT o.*, m.category AS category, m.name AS dish_name "
        "FROM orders o JOIN menu_items m ON o.menu_item_id = m.id "
        "WHERE o.status = 'ordered' AND o.cancel_requested_at IS NOT NULL "
        "ORDER BY o.cancel_requested_at"
    ).fetchall()
    today = today_jst()
    out = []
    for r in rows:
        row = dict(r)
        row["item_display"] = item_display_name(r["category"], r["dish_name"])
        order_date = date.fromisoformat(r["order_date"])
        # "その日を過ぎてしまった" requests are the ones that cost money.
        row["is_overdue"] = order_date < today
        row["is_today"] = order_date == today
        out.append(row)
    return out


# ---------- Week / deadline helpers ----------

def week_monday(d):
    return d - timedelta(days=d.weekday())


def week_deadline_dt(monday, settings):
    """Automatic default deadline for a week (Mon..Fri): `cutoff_weekday` of
    the PREVIOUS week. Used when no manual override exists for that week."""
    prev_monday = monday - timedelta(days=7)
    deadline_date = prev_monday + timedelta(days=settings["cutoff_weekday"])
    h, m = [int(x) for x in settings["cutoff_time"].split(":")]
    return datetime.combine(deadline_date, time(hour=h, minute=m))


def get_week_deadline_override(db, monday):
    """Return the manually-set deadline datetime for this week, or None if
    the week should use the automatic default."""
    row = db.execute(
        "SELECT deadline_at FROM week_deadlines WHERE monday = ?", (monday.isoformat(),)
    ).fetchone()
    if row:
        return datetime.fromisoformat(row["deadline_at"])
    return None


def get_week_deadline(db, monday, settings):
    """Effective deadline for a week: the manual override if the admin set
    one when registering that week's menu, otherwise the automatic default."""
    return get_week_deadline_override(db, monday) or week_deadline_dt(monday, settings)


def week_is_open(db, monday, settings, now=None):
    now = now or now_jst()
    return now < get_week_deadline(db, monday, settings)


def build_week_label(monday):
    friday = monday + timedelta(days=4)
    return f"{monday.month}/{monday.day}({WEEKDAY_JP[monday.weekday()]}) 〜 {friday.month}/{friday.day}({WEEKDAY_JP[friday.weekday()]})"


def split_dates_today_future_past(date_keys, today):
    """date_keys: an iterable of 'YYYY-MM-DD' strings. Splits into
    (today_key_or_None, future_dates_ascending, past_dates_descending) —
    the shared shape behind both admin date-group listings (注文一覧 and
    当日注文数確認): today stands alone, future is browsed soonest-first,
    past is browsed most-recent-first."""
    today_str = today.isoformat()
    keys = list(date_keys)
    today_key = today_str if today_str in keys else None
    future_dates = sorted(d for d in keys if d > today_str)
    past_dates = sorted((d for d in keys if d < today_str), reverse=True)
    return today_key, future_dates, past_dates


def group_dates_by_week(date_list):
    """Ascending date strings -> [{"monday","label","dates":[...]}, ...],
    one entry per Mon-Fri week, in the same order as date_list."""
    weeks = []
    current = None
    for d_str in date_list:
        monday = week_monday(date.fromisoformat(d_str))
        monday_str = monday.isoformat()
        if current is None or current["monday"] != monday_str:
            current = {"monday": monday_str, "label": build_week_label(monday), "dates": []}
            weeks.append(current)
        current["dates"].append(d_str)
    return weeks


def group_dates_by_month(date_list):
    """Date strings (any single order, consecutive same-month runs grouped)
    -> [{"month_key","label","dates":[...]}, ...]. Used with past_dates,
    which are already sorted most-recent-first."""
    months = []
    current = None
    for d_str in date_list:
        month_key = d_str[:7]
        if current is None or current["month_key"] != month_key:
            current = {"month_key": month_key, "label": f"{int(month_key[:4])}年{int(month_key[5:7])}月", "dates": []}
            months.append(current)
        current["dates"].append(d_str)
    return months


def _month_start(d):
    return d.replace(day=1)


def _next_month_start(d):
    return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)


def _prev_month_start(d):
    return date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)


def payroll_cycle(d):
    """The 16th-to-15th cycle containing date d, matching the real paper
    チケット受け渡し表's cutoff (closed on the 15th, deducted from that
    month's salary). Purely a same-cycle grouping for the employee's own
    order-history view — NOT the source of truth for actual ticket
    handouts, which stay on paper."""
    if d.day <= 15:
        end = d.replace(day=15)
        start = _prev_month_start(d).replace(day=16)
    else:
        start = d.replace(day=16)
        end = _next_month_start(d).replace(day=15)
    return start, end


def employee_ticket_status(db, employee_name, today):
    """Ticket status from the logged 受け渡し記録 (admin_issue_tickets): the
    last booklet handed out, minus bento actually picked up since that
    handout (order_date <= today, so not-yet-happened future orders aren't
    counted as used yet).

    The handout date itself is EXCLUDED from "used" (order_date strictly
    after issued_at): a new booklet is only ever given after the old one
    ran out, so if there's already an order on the handout date, that
    order was fulfilled with the old booklet, not the fresh one — counting
    it here would make a brand-new booklet show as already down a ticket."""
    last_issuance = db.execute(
        "SELECT * FROM ticket_issuances WHERE employee_name = ? ORDER BY issued_at DESC, id DESC LIMIT 1",
        (employee_name,),
    ).fetchone()
    if not last_issuance:
        return {"known": False}
    used_since_issuance = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE employee_name = ? AND status = 'ordered' "
        "AND order_date > ? AND order_date <= ?",
        (employee_name, last_issuance["issued_at"], today.isoformat()),
    ).fetchone()["c"]
    return {
        "known": True,
        "remaining": max(0, last_issuance["quantity"] - used_since_issuance),
        "total": last_issuance["quantity"],
        "issued_at": last_issuance["issued_at"],
    }


def item_display_name(category, name):
    label = CATEGORY_LABELS.get(category, category)
    return f"{label}({name})" if name else label


MONTH_DAYS = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  # Feb=29 to allow leap-day birthdays


def normalize_birthday(raw):
    """'0517' (or '5/17', '5-17') -> '0517' if it's a real month/day, else None."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) != 4:
        return None
    month, day = int(digits[:2]), int(digits[2:])
    if not (1 <= month <= 12) or not (1 <= day <= MONTH_DAYS[month - 1]):
        return None
    return digits


def looks_like_full_name(name):
    """Requires a space between family and given name (like the 'like:山田
    太郎' example), since surname-only names collide too often between
    employees to safely tell people apart."""
    return " " in name or "　" in name


# ---------- Employee routes ----------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        name = request.form.get("employee_name", "").strip()
        birthday_raw = request.form.get("birthday", "").strip()

        if not name:
            flash("氏名を入力してください。", "error")
            return redirect(url_for("index"))
        if not looks_like_full_name(name):
            flash("苗字だけでなく、フルネームで入力してください(例: 山田 太郎)。", "error")
            return redirect(url_for("index"))
        birthday = normalize_birthday(birthday_raw)
        if not birthday:
            flash("生年月日(月日)を4桁で入力してください(例: 5月17日→0517)。", "error")
            return redirect(url_for("index"))

        db = get_db()
        existing = db.execute("SELECT birthday FROM employees WHERE name = ?", (name,)).fetchone()
        if existing:
            if existing["birthday"] != birthday:
                flash("氏名と生年月日の組み合わせが一致しません。ご本人の生年月日を入力してください。", "error")
                return redirect(url_for("index"))
        else:
            db.execute("INSERT INTO employees (name, birthday) VALUES (?, ?)", (name, birthday))
            db.commit()

        session["employee_name"] = name
        return redirect(url_for("index"))

    employee_name = session.get("employee_name")
    if not employee_name:
        db = get_db()
        known_employees = [
            r["employee_name"]
            for r in db.execute("SELECT DISTINCT employee_name FROM orders ORDER BY employee_name").fetchall()
        ]
        return render_template("login.html", known_employees=known_employees)

    db = get_db()
    settings = get_settings()
    today = today_jst()

    # This week's full Mon-Fri picture, INCLUDING already-passed days —
    # deliberately not reusing menu_by_date/_load_employee_menu_context
    # (those only look from today onward), since "what did I already order
    # this week" needs Monday and Tuesday to still show up on a Wednesday.
    this_monday = week_monday(today)
    this_friday = this_monday + timedelta(days=4)
    this_week_days = _week_glance(db, employee_name, this_monday)

    # Next week: has anything been ordered yet, and is it still open — the
    # "did I forget to order for next week" check, most relevant right
    # around the Friday 15:00 deadline.
    next_monday = this_monday + timedelta(days=7)
    next_week_days = _week_glance(db, employee_name, next_monday)
    next_week_menu_exists = any(d["cats"] for d in next_week_days)
    next_week_ordered = sum(1 for d in next_week_days if d["selected_code"])
    # 定休日 can't be ordered, so "all ordered" is measured against the days
    # that actually have a bento — otherwise a week containing a holiday would
    # permanently read as "一部のみ" and nag someone who ordered everything.
    next_week_orderable = sum(1 for d in next_week_days if not d["closed_reason"])
    next_week_status = {
        "label": build_week_label(next_monday),
        "menu_exists": next_week_menu_exists,
        "open": week_is_open(db, next_monday, settings),
        "deadline_str": get_week_deadline(db, next_monday, settings).strftime("%m/%d(%a) %H:%M"),
        "ordered_count": next_week_ordered,
        "orderable_count": next_week_orderable,
    }

    ticket_status = employee_ticket_status(db, employee_name, today)

    # This payroll cycle's deduction: one ¥3,000 charge per booklet handed
    # out within the cycle (billed on handout date, not on ticket use — see
    # payroll_cycle()'s docstring), from the 受け渡し記録 table itself.
    # Falls back to showing the order count as a reference only if nothing
    # has been logged yet for this cycle.
    price_per_booklet = settings["price"] * 10
    cycle_start, cycle_end = payroll_cycle(today)
    cycle_booklets = db.execute(
        "SELECT COUNT(*) as c FROM ticket_issuances WHERE employee_name = ? "
        "AND issued_at >= ? AND issued_at <= ?",
        (employee_name, cycle_start.isoformat(), cycle_end.isoformat()),
    ).fetchone()["c"]
    cycle_order_count = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE employee_name = ? AND status = 'ordered' "
        "AND order_date >= ? AND order_date <= ?",
        (employee_name, cycle_start.isoformat(), cycle_end.isoformat()),
    ).fetchone()["c"]
    cycle_issuance_dates = [
        r["issued_at"]
        for r in db.execute(
            "SELECT issued_at FROM ticket_issuances WHERE employee_name = ? "
            "AND issued_at >= ? AND issued_at <= ? ORDER BY issued_at",
            (employee_name, cycle_start.isoformat(), cycle_end.isoformat()),
        ).fetchall()
    ]
    cycle_summary = {
        "label": f"{cycle_start.month}/{cycle_start.day}〜{cycle_end.month}/{cycle_end.day}",
        "booklets": cycle_booklets,
        "total": cycle_booklets * price_per_booklet,
        "price_per_booklet": price_per_booklet,
        "order_count": cycle_order_count,
        "issuance_dates": cycle_issuance_dates,
    }

    return render_template(
        "dashboard.html",
        employee_name=employee_name,
        this_week_label=build_week_label(this_monday),
        this_week_days=this_week_days,
        next_week_status=next_week_status,
        next_week_days=next_week_days,
        ticket_status=ticket_status,
        cycle_summary=cycle_summary,
        category_labels=CATEGORY_LABELS,
        today=today.isoformat(),
    )


def _week_glance(db, employee_name, monday):
    """One employee's Mon-Fri picture for the week starting `monday`: menu
    (if registered) + what they've selected, regardless of whether those
    dates are in the past, today, or the future. Used for the dashboard's
    this-week/next-week summaries, which — unlike the ordering page — need
    to show already-passed days too."""
    friday = monday + timedelta(days=4)
    today = today_jst()
    menu_rows = db.execute(
        "SELECT * FROM menu_items WHERE item_date >= ? AND item_date <= ?",
        (monday.isoformat(), friday.isoformat()),
    ).fetchall()
    menu_by_date = {}
    for row in menu_rows:
        menu_by_date.setdefault(row["item_date"], {})[row["category"]] = row

    order_rows = db.execute(
        "SELECT * FROM orders WHERE employee_name = ? AND order_date >= ? AND order_date <= ? "
        "AND status = 'ordered'",
        (employee_name, monday.isoformat(), friday.isoformat()),
    ).fetchall()
    orders_by_date = {o["order_date"]: o for o in order_rows}
    closed_days = get_closed_days(db, monday.isoformat(), friday.isoformat())

    now = now_jst()
    days = []
    for i in range(5):
        d = monday + timedelta(days=i)
        d_str = d.isoformat()
        cats = {} if d_str in closed_days else menu_by_date.get(d_str, {})
        order = orders_by_date.get(d_str)
        selected_item_id = order["menu_item_id"] if order else None
        selected_code = None
        for code, item in cats.items():
            if item["id"] == selected_item_id:
                selected_code = code
        is_today = d == today
        is_past = d < today
        cancel_requested = bool(order and order["cancel_requested_at"])
        # Mirrors order_cancel_day()'s own rules: a past day can't be
        # cancelled (already happened), and today only counts down to the
        # 9:00 same-day cutoff — after that, changes go through the admin's
        # morning process instead.
        can_cancel = (
            selected_code is not None
            and not is_past
            and not cancel_requested
            and (not is_today or now.time() < SAME_DAY_CANCEL_CUTOFF)
        )
        days.append({
            "date": d_str,
            "mmdd": f"{d.month}/{d.day}",
            "weekday": WEEKDAY_JP[d.weekday()],
            "cats": cats,
            "closed_reason": closed_days.get(d_str),
            "selected_code": selected_code,
            "is_today": is_today,
            "is_past": is_past,
            "paid": bool(order["paid"]) if order else False,
            "cancel_requested": cancel_requested,
            "can_cancel": can_cancel,
        })
    return days


def _load_employee_menu_context(db, employee_name, today):
    """Shared by the dashboard (quick_status) and the order page
    (weeks_info): this employee's upcoming menu + their current selections."""
    menu_rows = db.execute(
        "SELECT * FROM menu_items WHERE item_date >= ? ORDER BY item_date",
        (today.isoformat(),),
    ).fetchall()

    menu_by_date = {}
    for row in menu_rows:
        menu_by_date.setdefault(row["item_date"], {})[row["category"]] = row

    my_orders = db.execute(
        "SELECT * FROM orders WHERE employee_name = ? AND order_date >= ? AND status = 'ordered'",
        (employee_name, today.isoformat()),
    ).fetchall()
    my_selection = {}  # order_date -> menu_item_id
    my_cancel_requested = set()  # order_dates with a pending cancel request
    for o in my_orders:
        my_selection[o["order_date"]] = o["menu_item_id"]
        if o["cancel_requested_at"]:
            my_cancel_requested.add(o["order_date"])

    return menu_by_date, my_selection, my_cancel_requested


@app.route("/order")
def order_page():
    employee_name = session.get("employee_name")
    if not employee_name:
        return redirect(url_for("index"))

    db = get_db()
    settings = get_settings()
    today = today_jst()
    menu_by_date, my_selection, my_cancel_requested = _load_employee_menu_context(db, employee_name, today)

    # Closed days still get a row on the ordering page (greyed out, no choices)
    # rather than being dropped: a silently missing weekday reads as "did I
    # forget to order Monday?", while an explicit 定休日 answers the question.
    closed_days = get_closed_days(db, today.isoformat(), "9999-12-31")

    # Group dates into weeks (Mon..Fri)
    weeks = {}
    for d_str in sorted(set(menu_by_date.keys()) | set(closed_days.keys())):
        d = date.fromisoformat(d_str)
        monday = week_monday(d)
        weeks.setdefault(monday, []).append(d)

    weeks_info = []
    for monday in sorted(weeks.keys()):
        days = []
        for d in weeks[monday]:
            d_str = d.isoformat()
            cats = {} if d_str in closed_days else menu_by_date.get(d_str, {})
            selected_item_id = my_selection.get(d_str)
            selected_code = None
            for code, item in cats.items():
                if item["id"] == selected_item_id:
                    selected_code = code
            days.append({
                "date": d_str,
                "mmdd": f"{d.month}/{d.day}",
                "weekday": WEEKDAY_JP[d.weekday()],
                "cats": cats,
                "closed_reason": closed_days.get(d_str),
                "selected_code": selected_code,
                "cancel_requested": d_str in my_cancel_requested,
                "is_past": d < today,
                "is_today": d == today,
            })
        deadline_dt = get_week_deadline(db, monday, settings)
        weeks_info.append({
            "monday": monday.isoformat(),
            "label": build_week_label(monday),
            "open": week_is_open(db, monday, settings),
            "is_current_week": monday == week_monday(today),
            "deadline_str": deadline_dt.strftime("%m/%d(%a) %H:%M"),
            "days": days,
        })

    return render_template(
        "order.html",
        employee_name=employee_name,
        weeks_info=weeks_info,
        price=settings["price"],
        category_codes=CATEGORY_CODES,
        category_labels=CATEGORY_LABELS,
        today=today.isoformat(),
    )


@app.route("/logout")
def logout():
    session.pop("employee_name", None)
    return redirect(url_for("index"))


@app.route("/order/week", methods=["POST"])
def order_week():
    employee_name = session.get("employee_name")
    if not employee_name:
        return redirect(url_for("index"))

    monday_str = request.form.get("week_monday")
    try:
        monday = date.fromisoformat(monday_str)
    except (TypeError, ValueError):
        return redirect(url_for("order_page"))

    db = get_db()
    settings = get_settings()

    if not week_is_open(db, monday, settings):
        flash("この週の注文受付は終了しています。当日分の追加・変更をご希望の場合は、当日9:00までに松浦さんか陽介さんまでメモでお伝えください。", "error")
        return redirect(url_for("order_page"))

    # A day can be marked closed after someone opened the ordering page, so
    # re-check here rather than trusting the form to only offer open days.
    closed_days = get_closed_days(db, monday.isoformat(), (monday + timedelta(days=4)).isoformat())

    for i in range(5):
        d = monday + timedelta(days=i)
        d_str = d.isoformat()
        field = f"day_{d_str}"
        if field not in request.form:
            continue
        if d_str in closed_days:
            continue
        choice = request.form.get(field)

        existing = db.execute(
            "SELECT * FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered'",
            (employee_name, d_str),
        ).fetchone()

        if choice == "none":
            if existing:
                db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (existing["id"],))
            continue

        if choice not in CATEGORY_CODES:
            continue

        menu_item = db.execute(
            "SELECT * FROM menu_items WHERE item_date = ? AND category = ?", (d_str, choice)
        ).fetchone()
        if not menu_item:
            continue

        if existing:
            if existing["menu_item_id"] != menu_item["id"]:
                db.execute(
                    "UPDATE orders SET menu_item_id = ?, unit_price = ?, created_at = ? WHERE id = ?",
                    (menu_item["id"], settings["price"], now_jst().isoformat(), existing["id"]),
                )
        else:
            try:
                db.execute(
                    "INSERT INTO orders (order_date, employee_name, menu_item_id, quantity, status, paid, "
                    "unit_price, created_by, created_at) VALUES (?, ?, ?, 1, 'ordered', 0, ?, 'self', ?)",
                    (d_str, employee_name, menu_item["id"], settings["price"], now_jst().isoformat()),
                )
            except sqlite3.IntegrityError:
                # Raced with another submit (double-click / two tabs) that just
                # created this day's order; fold into an update, not a 500.
                dup = db.execute(
                    "SELECT id FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered'",
                    (employee_name, d_str),
                ).fetchone()
                if dup:
                    db.execute(
                        "UPDATE orders SET menu_item_id = ?, unit_price = ?, created_at = ? WHERE id = ?",
                        (menu_item["id"], settings["price"], now_jst().isoformat(), dup["id"]),
                    )

    db.commit()
    flash("注文を更新しました。", "success")
    return redirect(url_for("order_page"))


@app.route("/order/cancel-day", methods=["POST"])
def order_cancel_day():
    """Let an employee flag their own order as "please cancel", for a future
    day (even after that week's ordering deadline has passed) or for today
    itself before the 9:00 same-day cutoff. This doesn't cancel right away —
    the admin approves it from the dashboard — because by the time the
    deadline has passed, the admin has usually already printed that week's
    paper checklist, and a silent in-app cancellation would leave the paper
    out of sync with reality. A day that has already passed can never be
    cancelled, and a same-day request past 9:00 is rejected since the
    admin's morning process has usually already started by then."""
    employee_name = session.get("employee_name")
    if not employee_name:
        return redirect(url_for("index"))

    order_date = request.form.get("order_date", "")
    today_str = today_jst().isoformat()
    if order_date < today_str:
        return redirect(request.referrer or url_for("index"))
    if order_date == today_str and now_jst().time() >= SAME_DAY_CANCEL_CUTOFF:
        flash("本日分のキャンセル希望は9:00までです。それ以降は松浦さんか陽介さんに直接お伝えください。", "error")
        return redirect(request.referrer or url_for("index"))

    db = get_db()
    existing = db.execute(
        "SELECT * FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered'",
        (employee_name, order_date),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE orders SET cancel_requested_at = ? WHERE id = ?",
            (now_jst().isoformat(), existing["id"]),
        )
        db.commit()
        flash(f"{order_date} のキャンセル希望を送信しました。松浦さんか陽介さんが確認後、正式にキャンセルされます。", "success")
    return redirect(request.referrer or url_for("index"))


@app.route("/my-orders")
def my_orders():
    employee_name = session.get("employee_name")
    if not employee_name:
        return redirect(url_for("index"))
    db = get_db()
    raw = db.execute(
        "SELECT o.*, m.category as category, m.name as dish_name "
        "FROM orders o JOIN menu_items m ON o.menu_item_id = m.id "
        "WHERE o.employee_name = ? AND o.status = 'ordered' "
        "ORDER BY o.order_date DESC",
        (employee_name,),
    ).fetchall()
    settings = get_settings()
    rows = []
    for r in raw:
        row = dict(r)
        row["item_display"] = item_display_name(r["category"], r["dish_name"])
        monday = week_monday(date.fromisoformat(r["order_date"]))
        row["cancellable"] = week_is_open(db, monday, settings)
        rows.append(row)

    # Grouped by the same 16th-to-15th cycle the real payroll ticket
    # deduction uses, newest cycle first — "how much/how many since the
    # last cutoff" is the question people actually have, not an all-time
    # flat list.
    cycles_by_key = {}
    for row in rows:
        start, end = payroll_cycle(date.fromisoformat(row["order_date"]))
        cycles_by_key.setdefault((start, end), []).append(row)

    cycles = []
    for (start, end) in sorted(cycles_by_key.keys(), reverse=True):
        cycle_rows = cycles_by_key[(start, end)]
        cycles.append({
            "label": f"{start.month}/{start.day}〜{end.month}/{end.day}",
            "orders": cycle_rows,
            "ticket_count": sum(r["quantity"] for r in cycle_rows),
            "total": sum(r["unit_price"] * r["quantity"] for r in cycle_rows),
            "unpaid_total": sum(r["unit_price"] * r["quantity"] for r in cycle_rows if not r["paid"]),
        })

    total = sum(r["unit_price"] * r["quantity"] for r in rows)
    unpaid_total = sum(r["unit_price"] * r["quantity"] for r in rows if not r["paid"])

    # A second, simpler view of the same orders for "what did I eat and
    # when" browsing: month, then week within the month, newest first
    # throughout (rows/order_dates are already DESC from the query above).
    rows_by_date = {r["order_date"]: r for r in rows}
    order_dates = [r["order_date"] for r in rows]
    history_months = group_dates_by_month(order_dates)
    for month in history_months:
        month["weeks"] = group_dates_by_week(month["dates"])
        for week in month["weeks"]:
            week["orders"] = [rows_by_date[d] for d in week["dates"]]

    return render_template(
        "my_orders.html",
        employee_name=employee_name,
        cycles=cycles,
        total=total,
        unpaid_total=unpaid_total,
        history_months=history_months,
    )


@app.route("/order/cancel", methods=["POST"])
def cancel_order():
    employee_name = session.get("employee_name")
    order_id = request.form.get("order_id")
    db = get_db()
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    settings = get_settings()
    if order and order["employee_name"] == employee_name:
        monday = week_monday(date.fromisoformat(order["order_date"]))
        if week_is_open(db, monday, settings):
            db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
            db.commit()
            flash("注文をキャンセルしました。", "success")
        else:
            flash("この週の注文受付は終了しているため、キャンセルできません。休みなどでキャンセルしたい場合は、当日9:00までに松浦さんか陽介さんまでメモでお伝えください。", "error")
    else:
        flash("この注文はキャンセルできません。", "error")
    return redirect(url_for("my_orders"))


# ---------- Admin routes ----------

def admin_required():
    return session.get("is_admin", False)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == ADMIN_PASSWORD:
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("パスワードが違います。", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login"))

    db = get_db()
    settings = get_settings()
    today = today_jst()
    today_str = today.isoformat()

    menu_rows = db.execute(
        "SELECT * FROM menu_items WHERE item_date >= ? ORDER BY item_date, category",
        (today_str,),
    ).fetchall()
    menu_by_date = {}
    for row in menu_rows:
        menu_by_date.setdefault(row["item_date"], []).append(row)

    upcoming_closed_days = sorted(get_closed_days(db, today_str, "9999-12-31").items())

    # Plain-dict version of menu_by_date (date -> {category: dish name}) for
    # the single-day proxy order form's JS: as the admin changes the date,
    # the 区分 dropdown updates to show that day's actual dish name instead
    # of just the category, so they can pick the same item the employee
    # would have ("フライあり: 唐揚げ") rather than a bare category.
    proxy_menu_map = {
        d: {item["category"]: item["name"] for item in items}
        for d, items in menu_by_date.items()
    }

    raw_orders = db.execute(
        "SELECT o.*, m.category as category, m.name as dish_name "
        "FROM orders o JOIN menu_items m ON o.menu_item_id = m.id "
        "WHERE o.status = 'ordered' ORDER BY o.order_date, o.employee_name"
    ).fetchall()
    orders = []
    for r in raw_orders:
        row = dict(r)
        row["item_display"] = item_display_name(r["category"], r["dish_name"])
        orders.append(row)

    # Employee-submitted "please cancel" requests, waiting for the admin to
    # approve — surfaced up top so they're seen regardless of which tab is
    # open or whether that date's group happens to be collapsed.
    cancel_requests = pending_cancel_requests(db)

    # Both 注文一覧 (per-person records) and 当日注文数確認 (per-dish
    # totals) share the same "today leads, future grouped by week, past
    # grouped by month" shape — otherwise either one just piles up months
    # of history above the dates anyone still cares about. Note: the
    # per-day payload dict key is "day_orders"/"s", never "items" — a dict
    # has its own builtin .items() method, which Jinja's dot-notation would
    # resolve before falling back to a same-named dict key, silently
    # breaking `day.items` in the template.
    orders_by_date = {}
    for o in orders:
        orders_by_date.setdefault(o["order_date"], []).append(o)

    today_key, future_dates, past_dates = split_dates_today_future_past(orders_by_date.keys(), today)
    today_order_items = orders_by_date.get(today_key, []) if today_key else []

    future_weeks = group_dates_by_week(future_dates)
    for week in future_weeks:
        week["date_groups"] = [{"date": d, "day_orders": orders_by_date[d]} for d in week["dates"]]
        week["count"] = sum(o["quantity"] for d in week["dates"] for o in orders_by_date[d])

    past_months = group_dates_by_month(past_dates)
    for month in past_months:
        month["date_groups"] = [{"date": d, "day_orders": orders_by_date[d]} for d in month["dates"]]
        month["count"] = sum(o["quantity"] for d in month["dates"] for o in orders_by_date[d])

    summary = {}
    for o in orders:
        d = o["order_date"]
        s = summary.setdefault(d, {"count": 0, "item_counts": {}, "item_names": {}, "cat_counts": {}})
        s["count"] += o["quantity"]
        s["item_counts"][o["item_display"]] = s["item_counts"].get(o["item_display"], 0) + o["quantity"]
        s["item_names"].setdefault(o["item_display"], []).append(o["employee_name"])
        s["cat_counts"][o["category"]] = s["cat_counts"].get(o["category"], 0) + o["quantity"]

    summary_today_key, summary_future_dates, summary_past_dates = split_dates_today_future_past(summary.keys(), today)
    today_summary = summary.get(summary_today_key) if summary_today_key else None

    summary_future_weeks = group_dates_by_week(summary_future_dates)
    for week in summary_future_weeks:
        week["date_groups"] = [{"date": d, "s": summary[d]} for d in week["dates"]]
        week["count"] = sum(summary[d]["count"] for d in week["dates"])

    summary_past_months = group_dates_by_month(summary_past_dates)
    for month in summary_past_months:
        month["date_groups"] = [{"date": d, "s": summary[d]} for d in month["dates"]]
        month["count"] = sum(summary[d]["count"] for d in month["dates"])

    known_employees = [
        r["employee_name"]
        for r in db.execute("SELECT DISTINCT employee_name FROM orders ORDER BY employee_name").fetchall()
    ]

    # Names that have registered a birthday for the login check, so an admin
    # can reset one if someone mistypes it on first login and locks
    # themselves out (birthday itself isn't shown here — the reset button
    # doesn't need it, and there's no reason to display it otherwise).
    registered_employees = [
        r["name"] for r in db.execute("SELECT name FROM employees ORDER BY name").fetchall()
    ]

    # Recent ticket handouts, newest first, for the チケット管理 tab's history
    # table (with a delete button per row to undo a mis-entered date/name).
    ticket_issuances = [
        dict(r) for r in db.execute(
            "SELECT * FROM ticket_issuances ORDER BY issued_at DESC, id DESC LIMIT 50"
        ).fetchall()
    ]

    # Payroll-deduction summary: every handout grouped into the same
    # 16th-to-15th cycle used for the employee-side dashboard card, broken
    # down per employee so whoever processes payroll can read each person's
    # deduction straight off this screen instead of counting rows by hand.
    price_per_booklet = settings["price"] * 10
    all_issuances = db.execute("SELECT * FROM ticket_issuances ORDER BY issued_at").fetchall()
    payroll_cycles_by_key = {}
    for row in all_issuances:
        start, end = payroll_cycle(date.fromisoformat(row["issued_at"]))
        cyc = payroll_cycles_by_key.setdefault((start, end), {})
        emp = cyc.setdefault(row["employee_name"], {"booklets": 0})
        emp["booklets"] += 1

    payroll_deduction_cycles = []
    for (cyc_start, cyc_end) in sorted(payroll_cycles_by_key.keys(), reverse=True):
        emp_map = payroll_cycles_by_key[(cyc_start, cyc_end)]
        employees_rows = [
            {"name": name, "booklets": info["booklets"], "total": info["booklets"] * price_per_booklet}
            for name, info in sorted(emp_map.items())
        ]
        payroll_deduction_cycles.append({
            "label": f"{cyc_start.month}/{cyc_start.day}〜{cyc_end.month}/{cyc_end.day}",
            "employees": employees_rows,
            "total_booklets": sum(e["booklets"] for e in employees_rows),
            "total_amount": sum(e["total"] for e in employees_rows),
        })

    # Each known name's current ticket status, for the 受け渡し記録 form's
    # employee dropdown — a plain-text field risked a handout landing on
    # the wrong person (e.g. a typo, or picking the wrong "田中"), and
    # showing remaining counts right in the list makes it obvious who's
    # actually run out and needs a new booklet.
    ticket_dropdown_options = []
    for name in known_employees:
        status = employee_ticket_status(db, name, today)
        if status["known"]:
            label = f"{name}(残り{status['remaining']}枚)"
            out_of_tickets = status["remaining"] <= 0
        else:
            label = f"{name}(受け渡し記録なし)"
            out_of_tickets = True
        ticket_dropdown_options.append({"name": name, "label": label, "out_of_tickets": out_of_tickets})
    # Whoever's out of tickets (or has no record at all) is the one an admin
    # actually needs to find in this list, so surface them first.
    ticket_dropdown_options.sort(key=lambda o: not o["out_of_tickets"])

    weekday_labels = list(enumerate(WEEKDAY_JP))

    # This card defaults to today but can be pointed at any date (e.g. to
    # check tomorrow's roster ahead of time), via ?check_date=YYYY-MM-DD.
    # Surfaced separately from `orders` (not just filtered from it) so
    # cancelled rows for that date still show up struck-through instead of
    # just disappearing, which was confusing admins doing the morning check.
    check_date_str = request.args.get("check_date", today_str)
    try:
        date.fromisoformat(check_date_str)
    except ValueError:
        check_date_str = today_str
    check_date_weekday = WEEKDAY_JP[date.fromisoformat(check_date_str).weekday()]

    today_orders_raw = db.execute(
        "SELECT o.*, m.category as category, m.name as dish_name "
        "FROM orders o JOIN menu_items m ON o.menu_item_id = m.id "
        "WHERE o.order_date = ? ORDER BY o.employee_name",
        (check_date_str,),
    ).fetchall()
    today_orders = []
    for r in today_orders_raw:
        row = dict(r)
        row["item_display"] = item_display_name(r["category"], r["dish_name"])
        today_orders.append(row)

    # Weeks derived from the registered menu (from today onward), each with
    # its effective deadline, so the admin can manually pin a deadline for a
    # given week right where that week's menu was registered, instead of
    # relying only on the automatic "previous week's cutoff weekday" rule.
    week_mondays = sorted({week_monday(date.fromisoformat(d)) for d in menu_by_date.keys()})
    week_deadlines = []
    for monday in week_mondays:
        override = get_week_deadline_override(db, monday)
        effective = override or week_deadline_dt(monday, settings)
        week_deadlines.append({
            "monday": monday.isoformat(),
            "label": build_week_label(monday),
            "deadline_input": effective.strftime("%Y-%m-%dT%H:%M"),
            "is_override": override is not None,
        })

    # Same weeks as `week_deadlines`, but shaped for the "1週間分まとめて代理
    # 登録" form: each day needs its per-category menu items so the form can
    # offer them as choices, the same way the employee's own weekly form does.
    proxy_weeks = []
    week_days = {}
    for d_str in sorted(menu_by_date.keys()):
        d = date.fromisoformat(d_str)
        week_days.setdefault(week_monday(d), []).append(d)
    for monday in sorted(week_days.keys()):
        days = []
        for d in week_days[monday]:
            d_str = d.isoformat()
            days.append({
                "date": d_str,
                "mmdd": f"{d.month}/{d.day}",
                "weekday": WEEKDAY_JP[d.weekday()],
                "cats": {item["category"]: item for item in menu_by_date[d_str]},
            })
        proxy_weeks.append({"monday": monday.isoformat(), "label": build_week_label(monday), "days": days})

    return render_template(
        "admin.html",
        menu_by_date=menu_by_date,
        upcoming_closed_days=upcoming_closed_days,
        orders=orders,
        cancel_requests=cancel_requests,
        today_order_items=today_order_items,
        future_weeks=future_weeks,
        past_months=past_months,
        today_orders=today_orders,
        check_date=check_date_str,
        check_date_weekday=check_date_weekday,
        is_check_date_today=(check_date_str == today_str),
        today_summary=today_summary,
        summary_future_weeks=summary_future_weeks,
        summary_past_months=summary_past_months,
        settings=settings,
        today=today_str,
        category_codes=CATEGORY_CODES,
        category_labels=CATEGORY_LABELS,
        category_short=CATEGORY_SHORT,
        known_employees=known_employees,
        registered_employees=registered_employees,
        ticket_issuances=ticket_issuances,
        ticket_dropdown_options=ticket_dropdown_options,
        payroll_deduction_cycles=payroll_deduction_cycles,
        proxy_menu_map=proxy_menu_map,
        weekday_labels=weekday_labels,
        week_deadlines=week_deadlines,
        proxy_weeks=proxy_weeks,
    )


def upsert_menu_item(db, item_date, code, name):
    """Create or overwrite the menu item for (item_date, code). No-op if name is blank."""
    if not name:
        return False
    existing = db.execute(
        "SELECT id FROM menu_items WHERE item_date = ? AND category = ?", (item_date, code)
    ).fetchone()
    if existing:
        db.execute("UPDATE menu_items SET name = ? WHERE id = ?", (name, existing["id"]))
    else:
        db.execute(
            "INSERT INTO menu_items (item_date, category, name) VALUES (?, ?, ?)",
            (item_date, code, name),
        )
    return True


@app.route("/admin/menu/add", methods=["POST"])
def admin_add_menu():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    item_date = request.form.get("item_date")
    added = 0
    for code in CATEGORY_CODES:
        name = request.form.get(f"name_{code}", "").strip()
        if upsert_menu_item(db, item_date, code, name):
            added += 1
    if added:
        set_day_open(db, item_date)
    db.commit()
    if added:
        flash(f"{item_date} のメニューを登録しました。", "success")
    else:
        flash("メニュー名を1つ以上入力してください。", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/pending")
def admin_pending():
    """Small JSON payload the open admin page polls, so a cancel request that
    arrives while 松浦さん is looking at another tab still surfaces (browser
    notification + tab title count) instead of waiting for a manual reload."""
    if not admin_required():
        return jsonify({"error": "unauthorized"}), 403
    db = get_db()
    pending = pending_cancel_requests(db)
    return jsonify({
        "count": len(pending),
        "overdue": sum(1 for p in pending if p["is_overdue"]),
        "today": sum(1 for p in pending if p["is_today"]),
        "names": [f"{p['employee_name']}さん({p['order_date'][5:]})" for p in pending[:5]],
        # The page compares this against its own clock to fire the 9:00
        # reminder on JST, not on whatever timezone the viewer's PC is set to.
        "now": now_jst().strftime("%H:%M"),
        "same_day_cutoff": SAME_DAY_CANCEL_CUTOFF.strftime("%H:%M"),
    })


@app.route("/admin/menu/close-day", methods=["POST"])
def admin_close_day():
    """Mark one date as 定休日 (or reopen it) from the メニュー登録 tab. The PDF
    import guesses this, but リリテイ's calendar isn't always readable, so the
    admin needs a way to fix it after the fact."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    item_date = request.form.get("item_date", "")
    try:
        date.fromisoformat(item_date)
    except ValueError:
        flash("日付の形式が正しくありません。", "error")
        return redirect(url_for("admin_dashboard"))

    if request.form.get("action") == "open":
        set_day_open(db, item_date)
        db.commit()
        flash(f"{item_date} を定休日ではなくしました。メニューを登録してください。", "success")
        return redirect(url_for("admin_dashboard"))

    # Existing orders for that date would be left stranded (the bento won't be
    # delivered), so make the admin resolve them rather than silently hiding it.
    live_orders = db.execute(
        "SELECT COUNT(*) AS c FROM orders WHERE order_date = ? AND status = 'ordered'",
        (item_date,),
    ).fetchone()["c"]
    if live_orders:
        flash(
            f"{item_date} には注文が{live_orders}件残っています。"
            "先に「チケット回収」タブでキャンセルしてから定休日にしてください。",
            "error",
        )
        return redirect(url_for("admin_dashboard"))

    set_day_closed(db, item_date, request.form.get("reason", "").strip() or DEFAULT_CLOSED_REASON)
    db.commit()
    flash(f"{item_date} を定休日にしました。社員は注文できなくなります。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/menu/import", methods=["POST"])
def admin_import_menu_pdf():
    """Step 1: admin uploads the monthly PDF. We parse it and show an editable
    review screen — nothing is saved to the live menu yet."""
    if not admin_required():
        return redirect(url_for("admin_login"))

    uploaded = request.files.get("pdf_file")
    if not uploaded or not uploaded.filename:
        flash("PDFファイルを選択してください。", "error")
        return redirect(url_for("admin_dashboard"))
    if not uploaded.filename.lower().endswith(".pdf"):
        flash("PDFファイルを選択してください。", "error")
        return redirect(url_for("admin_dashboard"))

    result, error = parse_menu_pdf(uploaded.stream)
    if error:
        flash(error, "error")
        return redirect(url_for("admin_dashboard"))

    today_str = today_jst().isoformat()
    # Don't offer to overwrite dates already in the past.
    dates = sorted(d for d in result.keys() if d >= today_str)
    if not dates:
        flash("PDFから読み取れた日付はすべて過去の日付でした。", "error")
        return redirect(url_for("admin_dashboard"))

    # Pre-tick the 定休日 box wherever the PDF wrote a closure marker into the
    # menu cells, so the admin confirms a guess instead of spotting it unaided.
    auto_closed = {
        d: looks_like_closed_day(result[d].values()) for d in dates
    }

    return render_template(
        "admin_import_review.html",
        dates=dates,
        parsed=result,
        auto_closed=auto_closed,
        category_codes=CATEGORY_CODES,
        category_labels=CATEGORY_LABELS,
    )


@app.route("/admin/menu/import/commit", methods=["POST"])
def admin_import_menu_commit():
    """Step 2: admin has reviewed/edited the extracted menu; save the checked
    dates to the live menu."""
    if not admin_required():
        return redirect(url_for("admin_login"))

    db = get_db()
    dates = request.form.getlist("dates")
    saved_count = 0
    closed_count = 0
    for d in dates:
        if not request.form.get(f"include_{d}"):
            continue
        if request.form.get(f"closed_{d}"):
            set_day_closed(db, d)
            closed_count += 1
            continue
        # Re-registering a day that was previously closed reopens it.
        set_day_open(db, d)
        any_added = False
        for code in CATEGORY_CODES:
            name = request.form.get(f"name_{d}_{code}", "").strip()
            if upsert_menu_item(db, d, code, name):
                any_added = True
        if any_added:
            saved_count += 1
    db.commit()
    if saved_count or closed_count:
        parts = []
        if saved_count:
            parts.append(f"{saved_count}日分のメニュー")
        if closed_count:
            parts.append(f"{closed_count}日分の定休日")
        flash("、".join(parts) + "を登録しました。", "success")
    else:
        flash("登録する日付が選択されていませんでした。", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/menu/delete", methods=["POST"])
def admin_delete_menu():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    menu_item_id = request.form.get("menu_item_id")
    # Any order referencing this item (even a cancelled one) will trip the
    # foreign-key constraint on DELETE, so check for all statuses, not just
    # 'ordered', and back it with a try/except as a safety net either way.
    in_use = db.execute(
        "SELECT COUNT(*) as c FROM orders WHERE menu_item_id = ?",
        (menu_item_id,),
    ).fetchone()["c"]
    if in_use:
        flash("この項目は注文履歴(キャンセル済みを含む)があるため削除できません。", "error")
    else:
        try:
            db.execute("DELETE FROM menu_items WHERE id = ?", (menu_item_id,))
            db.commit()
            flash("メニューを削除しました。", "success")
        except sqlite3.IntegrityError:
            flash("この項目は注文履歴があるため削除できません。", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/menu/update", methods=["POST"])
def admin_update_menu_item():
    """Rename a registered menu item in place. Existing orders reference it
    by menu_item_id and look up the name live via JOIN, so this immediately
    corrects the name everywhere it's displayed (past and future)."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    menu_item_id = request.form.get("menu_item_id")
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("メニュー名を入力してください。", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute("UPDATE menu_items SET name = ? WHERE id = ?", (name, menu_item_id))
    db.commit()
    flash("メニュー名を更新しました。", "success")
    return redirect(url_for("admin_dashboard"))


def admin_upsert_order(db, settings, order_date, employee_name, category):
    """Create or overwrite the employee's active order for one day, as the
    admin. Shared by the single-day and whole-week proxy forms. Returns True
    if a menu item existed for (order_date, category) and the write happened."""
    menu_item = db.execute(
        "SELECT * FROM menu_items WHERE item_date = ? AND category = ?", (order_date, category)
    ).fetchone()
    if not menu_item:
        return False

    existing = db.execute(
        "SELECT * FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered'",
        (employee_name, order_date),
    ).fetchone()
    if existing:
        db.execute(
            "UPDATE orders SET menu_item_id = ?, unit_price = ?, created_at = ?, created_by = 'admin' WHERE id = ?",
            (menu_item["id"], settings["price"], now_jst().isoformat(), existing["id"]),
        )
    else:
        try:
            db.execute(
                "INSERT INTO orders (order_date, employee_name, menu_item_id, quantity, status, paid, "
                "unit_price, created_by, created_at) VALUES (?, ?, ?, 1, 'ordered', 0, ?, 'admin', ?)",
                (order_date, employee_name, menu_item["id"], settings["price"], now_jst().isoformat()),
            )
        except sqlite3.IntegrityError:
            # Raced with another active order for the same person/day; update it.
            dup = db.execute(
                "SELECT id FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered'",
                (employee_name, order_date),
            ).fetchone()
            if dup:
                db.execute(
                    "UPDATE orders SET menu_item_id = ?, unit_price = ?, created_at = ?, created_by = 'admin' WHERE id = ?",
                    (menu_item["id"], settings["price"], now_jst().isoformat(), dup["id"]),
                )
    return True


@app.route("/admin/orders/proxy-add", methods=["POST"])
def admin_proxy_add_order():
    """Admin enters an order on behalf of an employee, bypassing the weekly deadline.
    Matches the real workflow: same-day requests are told to the admin directly."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    settings = get_settings()
    order_date = request.form.get("order_date")
    employee_name = request.form.get("employee_name", "").strip()
    category = request.form.get("category")

    if not order_date or not employee_name or category not in CATEGORY_CODES:
        flash("日付・氏名・区分をすべて入力してください。", "error")
        return redirect(url_for("admin_dashboard"))

    if not admin_upsert_order(db, settings, order_date, employee_name, category):
        flash("その日・区分のメニューが登録されていません。先にメニューを登録してください。", "error")
        return redirect(url_for("admin_dashboard"))
    db.commit()
    flash(f"{employee_name} さんの注文を登録しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/proxy-add-week", methods=["POST"])
def admin_proxy_add_week():
    """Admin registers a whole Mon-Fri week for one named person in one go,
    instead of submitting the single-day form five times. Bypasses the
    deadline the same way the single-day proxy form does."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    settings = get_settings()
    employee_name = request.form.get("employee_name", "").strip()
    monday_str = request.form.get("week_monday")

    if not employee_name or not monday_str:
        flash("氏名と週を指定してください。", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        monday = date.fromisoformat(monday_str)
    except ValueError:
        flash("週の指定が正しくありません。", "error")
        return redirect(url_for("admin_dashboard"))

    saved = 0
    for i in range(5):
        d_str = (monday + timedelta(days=i)).isoformat()
        category = request.form.get(f"day_{d_str}")
        if not category or category not in CATEGORY_CODES:
            continue
        if admin_upsert_order(db, settings, d_str, employee_name, category):
            saved += 1
    db.commit()
    if saved:
        flash(f"{employee_name} さんの{saved}日分の注文を登録しました。", "success")
    else:
        flash("登録する区分が選択されていませんでした。", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/employees/reset-birthday", methods=["POST"])
def admin_reset_employee_birthday():
    """Forget a name's registered birthday (e.g. typo on first login, or the
    person forgot it) so they can register it fresh next time they log in."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    name = request.form.get("employee_name", "").strip()
    db.execute("DELETE FROM employees WHERE name = ?", (name,))
    db.commit()
    flash(f"{name} さんの生年月日の登録をリセットしました。次回ログイン時に再登録されます。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/tickets/issue", methods=["POST"])
def admin_issue_tickets():
    """Log that a fresh 10-ticket booklet was handed to someone — the digital
    twin of writing a line in the paper チケット受け渡し表. This is the only
    source of truth for 残り枚数/天引き予定額 on the employee dashboard."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    name = request.form.get("employee_name", "").strip()
    issued_at = request.form.get("issued_at", "").strip()
    if not name or not issued_at:
        flash("氏名と日付を入力してください。", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        date.fromisoformat(issued_at)
    except ValueError:
        flash("日付の形式が正しくありません。", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute(
        "INSERT INTO ticket_issuances (employee_name, issued_at, quantity, created_at) VALUES (?, ?, 10, ?)",
        (name, issued_at, now_jst().isoformat()),
    )
    db.commit()
    flash(f"{name} さんに{issued_at}付でチケット10枚を渡した記録を保存しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/tickets/delete", methods=["POST"])
def admin_delete_ticket_issuance():
    """Undo a mis-entered 受け渡し記録 (wrong date, wrong name, duplicate)."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    issuance_id = request.form.get("issuance_id")
    db.execute("DELETE FROM ticket_issuances WHERE id = ?", (issuance_id,))
    db.commit()
    flash("受け渡し記録を削除しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/cancel", methods=["POST"])
def admin_cancel_order():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (request.form.get("order_id"),))
    db.commit()
    flash("注文をキャンセルしました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/approve-cancel", methods=["POST"])
def admin_approve_cancel_request():
    """Fulfil an employee's self-service cancellation request: actually
    cancels the order. Left as a separate step from the employee's request
    (rather than auto-cancelling) so the admin notices it and can update the
    already-printed paper checklist at the same time."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute(
        "UPDATE orders SET status = 'cancelled', cancel_requested_at = NULL WHERE id = ?",
        (request.form.get("order_id"),),
    )
    db.commit()
    flash("キャンセル希望を承認し、注文をキャンセルしました。紙のチェック表もあわせて修正してください。", "warning")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/reject-cancel", methods=["POST"])
def admin_reject_cancel_request():
    """Dismiss an employee's cancellation request without cancelling the
    order (e.g. the admin already confirmed with them it's not needed)."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    db.execute(
        "UPDATE orders SET cancel_requested_at = NULL WHERE id = ?",
        (request.form.get("order_id"),),
    )
    db.commit()
    flash("キャンセル希望を却下しました(注文はそのまま残っています)。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/bulk-cancel", methods=["POST"])
def admin_bulk_cancel():
    """Cancel several people's orders in one submit, for the morning
    attendance check on 本日の注文 — checking off several no-shows found via
    word of mouth or Salesforce Chatter shouldn't take one click-and-confirm
    each."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    order_ids = request.form.getlist("order_ids")
    if not order_ids:
        flash("キャンセルする人を選択してください。", "error")
        return redirect(url_for("admin_dashboard"))
    placeholders = ",".join("?" * len(order_ids))
    db.execute(f"UPDATE orders SET status = 'cancelled' WHERE id IN ({placeholders})", order_ids)
    db.commit()
    flash(f"{len(order_ids)}件の注文をキャンセルしました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/restore", methods=["POST"])
def admin_restore_order():
    """Undo a cancellation. Refuses if the employee already has a separate
    'ordered' row for that date (e.g. they re-ordered after being cancelled),
    since restoring would otherwise create two live orders for the same day."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    order_id = request.form.get("order_id")
    order = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        flash("対象の注文が見つかりません。", "error")
        return redirect(url_for("admin_dashboard"))
    conflict = db.execute(
        "SELECT id FROM orders WHERE employee_name = ? AND order_date = ? AND status = 'ordered' AND id != ?",
        (order["employee_name"], order["order_date"], order_id),
    ).fetchone()
    if conflict:
        flash(f"{order['employee_name']} さんは既にこの日の別の注文があるため元に戻せません。", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        db.execute("UPDATE orders SET status = 'ordered' WHERE id = ?", (order_id,))
        db.commit()
    except sqlite3.IntegrityError:
        # The unique index caught a same-day active order created between the
        # conflict check above and here.
        flash(f"{order['employee_name']} さんは既にこの日の別の注文があるため元に戻せません。", "error")
        return redirect(url_for("admin_dashboard"))
    flash(f"{order['employee_name']} さんの注文を元に戻しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/orders/toggle-paid", methods=["POST"])
def admin_toggle_paid():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    order_id = request.form.get("order_id")
    row = db.execute("SELECT paid FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row:
        db.execute("UPDATE orders SET paid = ? WHERE id = ?", (0 if row["paid"] else 1, order_id))
        db.commit()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/settings/update", methods=["POST"])
def admin_update_settings():
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    price = int(request.form.get("price", 300))
    cutoff_weekday = int(request.form.get("cutoff_weekday", 4))
    cutoff_time = request.form.get("cutoff_time", "15:00")
    db.execute(
        "UPDATE settings SET price = ?, cutoff_weekday = ?, cutoff_time = ? WHERE id = 1",
        (price, cutoff_weekday, cutoff_time),
    )
    db.commit()
    flash("設定を更新しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/week-deadline/set", methods=["POST"])
def admin_set_week_deadline():
    """Manually pin the deadline for one specific week, overriding the
    automatic default. Meant to be set right after that week's menu is
    registered, so exceptions (holidays, short weeks, etc.) don't have to
    fit the one-size-fits-all weekday/time rule in 設定."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    monday = request.form.get("monday")
    deadline_at = request.form.get("deadline_at")
    if not monday or not deadline_at:
        flash("締切日時を入力してください。", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        date.fromisoformat(monday)
        datetime.fromisoformat(deadline_at)
    except ValueError:
        flash("締切日時の形式が正しくありません。", "error")
        return redirect(url_for("admin_dashboard"))
    db.execute(
        "INSERT INTO week_deadlines (monday, deadline_at) VALUES (?, ?) "
        "ON CONFLICT(monday) DO UPDATE SET deadline_at = excluded.deadline_at",
        (monday, deadline_at),
    )
    db.commit()
    flash(f"{monday} の週の締切を手動で設定しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/week-deadline/reset", methods=["POST"])
def admin_reset_week_deadline():
    """Remove the manual override for a week, reverting it to the automatic
    default computed from 設定."""
    if not admin_required():
        return redirect(url_for("admin_login"))
    db = get_db()
    monday = request.form.get("monday")
    db.execute("DELETE FROM week_deadlines WHERE monday = ?", (monday,))
    db.commit()
    flash("自動計算の締切に戻しました。", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/print-checklist")
def admin_print_checklist():
    """A4-print-friendly checklist for a given day: name + menu + a blank box
    for the physical レ点 (checkmark) as bento are handed out. This stays a
    paper process on purpose — the app only produces the printable list."""
    if not admin_required():
        return redirect(url_for("admin_login"))

    db = get_db()
    target_date = request.args.get("date") or today_jst().isoformat()
    try:
        d = date.fromisoformat(target_date)
    except ValueError:
        d = today_jst()
        target_date = d.isoformat()

    raw = db.execute(
        "SELECT o.*, m.category as category, m.name as dish_name "
        "FROM orders o JOIN menu_items m ON o.menu_item_id = m.id "
        "WHERE o.order_date = ? AND o.status = 'ordered' "
        "ORDER BY o.employee_name",
        (target_date,),
    ).fetchall()

    rows = []
    counts = {}
    for r in raw:
        row = dict(r)
        row["item_display"] = item_display_name(r["category"], r["dish_name"])
        rows.append(row)
        counts[row["item_display"]] = counts.get(row["item_display"], 0) + row["quantity"]

    return render_template(
        "admin_print_checklist.html",
        target_date=target_date,
        weekday=WEEKDAY_JP[d.weekday()],
        rows=rows,
        counts=counts,
        total=len(rows),
    )


@app.route("/admin/print-checklist-week")
def admin_print_checklist_week():
    """One A4-friendly sheet covering the whole Mon-Fri week: rows are
    employees, columns are days — same shape as the old paper board, so one
    print job replaces printing a separate slip every day. Same-day
    additions (told to the admin verbally, after the sheet is already
    printed) are meant to be handwritten straight onto this sheet: existing
    employees just get a mark added in that day's empty cell, and brand-new
    same-day names go in the blank rows at the bottom."""
    if not admin_required():
        return redirect(url_for("admin_login"))

    db = get_db()
    monday_param = request.args.get("monday")
    try:
        base_date = date.fromisoformat(monday_param) if monday_param else today_jst()
    except ValueError:
        base_date = today_jst()
    monday = week_monday(base_date)
    days = [monday + timedelta(days=i) for i in range(5)]
    day_strs = [d.isoformat() for d in days]
    placeholders = ",".join("?" * len(day_strs))

    menu_rows = db.execute(
        f"SELECT * FROM menu_items WHERE item_date IN ({placeholders})",
        day_strs,
    ).fetchall()
    menu_by_date = {}
    for row in menu_rows:
        menu_by_date.setdefault(row["item_date"], {})[row["category"]] = row["name"]

    order_rows = db.execute(
        "SELECT o.*, m.category as category FROM orders o "
        f"JOIN menu_items m ON o.menu_item_id = m.id "
        f"WHERE o.order_date IN ({placeholders}) AND o.status = 'ordered'",
        day_strs,
    ).fetchall()

    matrix = {}
    day_counts = {d: 0 for d in day_strs}
    # Per-category counts too (not just the day total), so the printed sheet
    # itself carries the numbers needed to check delivered tickets against
    # the order — no need to go back to the 集計サマリー screen for that.
    cat_counts = {d: {} for d in day_strs}
    for r in order_rows:
        matrix.setdefault(r["employee_name"], {})[r["order_date"]] = r["category"]
        day_counts[r["order_date"]] += r["quantity"]
        cc = cat_counts[r["order_date"]]
        cc[r["category"]] = cc.get(r["category"], 0) + r["quantity"]

    employees = sorted(matrix.keys())

    day_infos = []
    for d, d_str in zip(days, day_strs):
        day_infos.append({
            "date": d_str,
            "mmdd": f"{d.month}/{d.day}",
            "weekday": WEEKDAY_JP[d.weekday()],
            "cats": menu_by_date.get(d_str, {}),
            "cat_counts": cat_counts[d_str],
            "count": day_counts[d_str],
        })

    return render_template(
        "admin_print_checklist_week.html",
        monday=monday.isoformat(),
        week_label=build_week_label(monday),
        day_infos=day_infos,
        day_strs=day_strs,
        employees=employees,
        matrix=matrix,
        category_codes=CATEGORY_CODES,
        category_labels=CATEGORY_LABELS,
        category_short=CATEGORY_SHORT,
        category_print_short=CATEGORY_PRINT_SHORT,
    )


# Runs on import too (not just `python app.py`), so gunicorn/WSGI deploys
# also get the tables created before the first request.
init_db()

if __name__ == "__main__":
    # Debug mode (the Werkzeug interactive debugger) is OFF by default because it
    # exposes a live Python console over the network on any unhandled error.
    # Set BENTO_DEBUG=1 only for local development on a machine you trust.
    debug_mode = os.environ.get("BENTO_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=5000, debug=debug_mode)
