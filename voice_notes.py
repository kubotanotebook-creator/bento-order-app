"""音声ファイル(または文字起こし済みテキスト)から議事録を自動生成し、Notionへ投稿する。

流れ:
  音声ファイル ──(ローカルWhisperで文字起こし)──▶ テキスト
  文字起こし済みテキストがあればそのまま利用(Whisperはスキップ)
  テキスト ──(ローカルLLM = Ollamaで要約・整形)──▶ 議事録
  議事録 ──▶ Notionデータベースに新規ページとして投稿

外部APIの課金を避けるため、文字起こし・要約はどちらもローカルで完結する
(faster-whisper / Ollama)。Notionへの投稿だけがNotion公式APIを叩く。

必要な環境変数:
  VOICE_NOTES_TOKEN     この機能(Web画面・Webhook)へのアクセストークン。
                        未設定の場合は機能自体を無効化する(誰でも使えると危険なため)。
  NOTION_API_KEY        Notion Integration のシークレットトークン
  NOTION_DATABASE_ID    議事録を追加するNotionデータベースのID
  NOTION_TITLE_PROPERTY タイトル列のプロパティ名(既定「名前」。DB側の実際の列名に合わせる)
  OLLAMA_BASE_URL       Ollama のURL(既定 http://localhost:11434)
  OLLAMA_MODEL          使用するモデル名(既定 llama3.1)
  WHISPER_MODEL_SIZE    faster-whisper のモデルサイズ(既定 small)
  WHISPER_COMPUTE_TYPE  faster-whisper の計算精度(既定 int8。CPUでも軽く動く)
"""
import json
import os
import urllib.error
import urllib.request

NOTION_VERSION = "2022-06-28"
NOTION_ENDPOINT = "https://api.notion.com/v1/pages"
NOTION_TIMEOUT_SECONDS = 15
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("VOICE_NOTES_OLLAMA_TIMEOUT", "300"))

_whisper_model = None  # 初回利用時にロードするシングルトン(起動を軽くするため)


class VoiceNotesError(Exception):
    """このモジュール内の処理失敗を、そのまま画面に出せるメッセージで表す。"""


def voice_notes_token():
    return os.environ.get("VOICE_NOTES_TOKEN", "").strip()


def is_enabled():
    return bool(voice_notes_token())


def notion_config():
    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    database_id = os.environ.get("NOTION_DATABASE_ID", "").strip()
    if not (api_key and database_id):
        return None
    return {
        "api_key": api_key,
        "database_id": database_id,
        "title_property": os.environ.get("NOTION_TITLE_PROPERTY", "名前").strip() or "名前",
    }


def is_notion_configured():
    return notion_config() is not None


def ollama_config():
    return {
        "base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"),
        "model": os.environ.get("OLLAMA_MODEL", "llama3.1"),
    }


def transcribe_audio(file_path):
    """faster-whisper で音声ファイルを日本語文字起こしする。"""
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise VoiceNotesError(
            "音声の文字起こしには faster-whisper が必要です。"
            "`pip install faster-whisper` を実行してください。"
        )

    if _whisper_model is None:
        model_size = os.environ.get("WHISPER_MODEL_SIZE", "small")
        compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
        _whisper_model = WhisperModel(model_size, compute_type=compute_type)

    segments, _info = _whisper_model.transcribe(file_path, language="ja")
    text = "".join(segment.text for segment in segments).strip()
    if not text:
        raise VoiceNotesError("文字起こし結果が空でした。音声ファイルを確認してください。")
    return text


MINUTES_PROMPT = """あなたは優秀な秘書です。以下は会議・メモの音声を文字起こししたテキストです。
これを読みやすい日本語の議事録に整形してください。次の見出し構成に必ず従い、\
見出し以外の前置きや後書きは書かないでください。

## 概要
(2〜3文で要約)

## 要点
- (箇条書き)

## 決定事項
- (箇条書き。なければ「特になし」)

## TODO
- (箇条書き。担当者が分かれば明記する。なければ「特になし」)

文字起こしテキスト:
---
{text}
---
"""


def summarize_to_minutes(raw_text):
    """ローカルLLM(Ollama)で文字起こしテキストを議事録形式に整形する。"""
    cfg = ollama_config()
    payload = {
        "model": cfg["model"],
        "prompt": MINUTES_PROMPT.format(text=raw_text),
        "stream": False,
    }
    req = urllib.request.Request(
        cfg["base_url"] + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise VoiceNotesError(
            f"ローカルLLM(Ollama)に接続できませんでした: {e}。"
            f"Ollamaが起動しているか、OLLAMA_BASE_URL(現在: {cfg['base_url']})を確認してください。"
        )
    text = (body.get("response") or "").strip()
    if not text:
        raise VoiceNotesError("要約結果が空でした。")
    return text


def _split_rich_text(text, limit=1900):
    """Notionの1リッチテキストあたりの文字数上限に収まるよう分割する。"""
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks or [""]


def _text_to_blocks(markdown_text):
    """簡易的な見出し(#)・箇条書き(-)だけを解釈してNotionブロックに変換する。"""
    blocks = []
    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("### "):
            block_type, content = "heading_3", line[4:]
        elif line.startswith("## "):
            block_type, content = "heading_2", line[3:]
        elif line.startswith("# "):
            block_type, content = "heading_1", line[2:]
        elif line.startswith(("- ", "* ")):
            block_type, content = "bulleted_list_item", line[2:]
        else:
            block_type, content = "paragraph", line

        for chunk in _split_rich_text(content):
            blocks.append({
                "object": "block",
                "type": block_type,
                block_type: {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
            })
    return blocks


def push_to_notion(title, minutes_text, raw_text=None):
    """議事録をNotionデータベースに新規ページとして追加し、ページURLを返す。"""
    cfg = notion_config()
    if not cfg:
        raise VoiceNotesError("Notionの環境変数(NOTION_API_KEY / NOTION_DATABASE_ID)が未設定です。")

    children = _text_to_blocks(minutes_text)
    if raw_text:
        children.append({
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [{"type": "text", "text": {"content": "元の文字起こし全文"}}],
                "children": [
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
                    }
                    for chunk in _split_rich_text(raw_text)[:90]
                ],
            },
        })

    payload = {
        "parent": {"database_id": cfg["database_id"]},
        "properties": {
            cfg["title_property"]: {"title": [{"text": {"content": title[:200]}}]},
        },
        # Notion APIは1リクエストにつき最大100ブロックまで
        "children": children[:100],
    }
    req = urllib.request.Request(
        NOTION_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Notion-Version": NOTION_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=NOTION_TIMEOUT_SECONDS) as res:
            body = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        raise VoiceNotesError(f"Notionへの投稿に失敗しました(HTTP {e.code}): {detail}")
    except urllib.error.URLError as e:
        raise VoiceNotesError(f"Notionへの投稿に失敗しました: {e}")

    return body.get("url")


def create_meeting_note(title, audio_path=None, raw_text=None):
    """音声ファイル、または文字起こし済みテキストから議事録を作りNotionへ投稿する。

    どちらか一方があればよい(raw_textがあればWhisperはスキップされる)。
    戻り値: (notion_url, minutes_text, raw_text)。失敗時は VoiceNotesError を投げる。
    """
    if not raw_text:
        if not audio_path:
            raise VoiceNotesError("音声ファイルか、文字起こし済みテキストのどちらかが必要です。")
        raw_text = transcribe_audio(audio_path)

    minutes_text = summarize_to_minutes(raw_text)
    notion_url = push_to_notion(title, minutes_text, raw_text=raw_text)
    return notion_url, minutes_text, raw_text
