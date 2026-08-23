"""
bento.db の定期バックアップ。

PythonAnywhere の「Tasks」(スケジュール実行) から1日1回呼ぶ想定:

    python3 /home/<ユーザー名>/bento-order-app/backup_db.py

稼働中のDBを shutil.copy で複製すると、書き込み途中の状態を掴んで壊れた
スナップショットになることがある。ここでは SQLite 公式のバックアップAPI
(Connection.backup) を使い、一貫性のある状態を取り出してから gzip 圧縮する。

環境変数:
  BENTO_DB_PATH        バックアップ元DB (app.py と同じ既定値)
  BENTO_BACKUP_DIR     保存先 (既定: DBと同じ場所の backups/)
  BENTO_BACKUP_KEEP    保持世代数 (既定: 30。1日1回なら約1か月分)
"""
import gzip
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("BENTO_DB_PATH", os.path.join(BASE_DIR, "bento.db"))
BACKUP_DIR = os.environ.get(
    "BENTO_BACKUP_DIR", os.path.join(os.path.dirname(DB_PATH), "backups")
)
KEEP = int(os.environ.get("BENTO_BACKUP_KEEP", "30"))

# ファイル名の日付は、アプリの日付表示と食い違わないようJSTで採る。
JST = ZoneInfo("Asia/Tokyo")
PREFIX = "bento-"
SUFFIX = ".db.gz"


def make_backup():
    """一貫性のあるスナップショットを取り、gzip 圧縮して保存する。"""
    stamp = datetime.now(JST).strftime("%Y-%m-%d_%H%M")
    dest = os.path.join(BACKUP_DIR, f"{PREFIX}{stamp}{SUFFIX}")

    # 先に一時ファイルへ backup API で吸い出し、それを圧縮する。
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db", dir=BACKUP_DIR)
    os.close(tmp_fd)
    try:
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            snapshot = sqlite3.connect(tmp_path)
            try:
                src.backup(snapshot)
            finally:
                snapshot.close()
        finally:
            src.close()

        with open(tmp_path, "rb") as f_in, gzip.open(dest, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    return dest


def prune():
    """古い世代を KEEP 件だけ残して削除する。削除した件数を返す。"""
    entries = sorted(
        f for f in os.listdir(BACKUP_DIR)
        if f.startswith(PREFIX) and f.endswith(SUFFIX)
    )
    removed = 0
    for name in entries[:-KEEP] if KEEP > 0 else []:
        os.remove(os.path.join(BACKUP_DIR, name))
        removed += 1
    return removed


def main():
    if not os.path.exists(DB_PATH):
        # タスクログに残す。異常終了させて気づけるようにする。
        print(f"ERROR: バックアップ元のDBが見つかりません: {DB_PATH}", file=sys.stderr)
        return 1

    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = make_backup()
    removed = prune()

    size_kb = os.path.getsize(dest) / 1024
    print(f"OK: {dest} ({size_kb:.1f} KB) を作成しました。古い世代を{removed}件削除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
