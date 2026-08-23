import os
import sys
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, g, request, session, redirect, url_for, render_template, flash

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
WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


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
    db.close()


def get_settings():
    db = get_db()
    row = db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return row


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


def item_display_name(category, name):
    label = CATEGORY_LABELS.get(category, category)
    return f"{label}({name})" if name else label


# ---------- Employee routes ----------

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        name = request.form.get("employee_name", "").strip()
        if name:
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
    for o in my_orders:
        my_selection[o["order_date"]] = o["menu_item_id"]

    # Quick at-a-glance status for the next couple of business days, shown at
    # the very top of the page so it's visible no matter why someone opened
    # the app — meant to catch "did I actually order today/tomorrow?" and
    # "I'm off tomorrow, did I remember to cancel?" before they scroll past it.
    quick_status = []
    for d_str in sorted(menu_by_date.keys())[:2]:
        if d_str < today.isoformat():
            continue
        d = date.fromisoformat(d_str)
        selected_item_id = my_selection.get(d_str)
        display = None
        if selected_item_id:
            for item in menu_by_date[d_str].values():
                if item["id"] == selected_item_id:
                    display = item_display_name(item["category"], item["name"])
        quick_status.append({
            "date": d_str,
            "weekday": WEEKDAY_JP[d.weekday()],
            "is_today": d == today,
            "ordered": display is not None,
            "display": display,
        })

    # Group dates into weeks (Mon..Fri)
    weeks = {}
    for d_str in sorted(menu_by_date.keys()):
        d = date.fromisoformat(d_str)
        monday = week_monday(d)
        weeks.setdefault(monday, []).append(d)

    weeks_info = []
    for monday in sorted(weeks.keys()):
        days = []
        for d in weeks[monday]:
            d_str = d.isoformat()
            cats = menu_by_date[d_str]
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
                "selected_code": selected_code,
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
        "index.html",
        employee_name=employee_name,
        weeks_info=weeks_info,
        quick_status=quick_status,
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
        return redirect(url_for("index"))

    db = get_db()
    settings = get_settings()

    if not week_is_open(db, monday, settings):
        flash("この週の注文受付は終了しています。当日分の追加・変更をご希望の場合は、当日9:00までに松浦さんか陽介さんまでメモでお伝えください。", "error")
        return redirect(url_for("index"))

    for i in range(5):
        d = monday + timedelta(days=i)
        d_str = d.isoformat()
        field = f"day_{d_str}"
        if field not in request.form:
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
    return redirect(url_for("index"))


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
    total = sum(r["unit_price"] * r["quantity"] for r in rows)
    unpaid_total = sum(r["unit_price"] * r["quantity"] for r in rows if not r["paid"])
    return render_template(
        "my_orders.html",
        employee_name=employee_name,
        orders=rows,
        total=total,
        unpaid_total=unpaid_total,
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

    summary = {}
    for o in orders:
        d = o["order_date"]
        s = summary.setdefault(d, {"count": 0, "item_counts": {}, "item_names": {}})
        s["count"] += o["quantity"]
        s["item_counts"][o["item_display"]] = s["item_counts"].get(o["item_display"], 0) + o["quantity"]
        s["item_names"].setdefault(o["item_display"], []).append(o["employee_name"])

    known_employees = [
        r["employee_name"]
        for r in db.execute("SELECT DISTINCT employee_name FROM orders ORDER BY employee_name").fetchall()
    ]

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

    return render_template(
        "admin.html",
        menu_by_date=menu_by_date,
        orders=orders,
        today_orders=today_orders,
        check_date=check_date_str,
        check_date_weekday=check_date_weekday,
        is_check_date_today=(check_date_str == today_str),
        summary=summary,
        settings=settings,
        today=today_str,
        category_codes=CATEGORY_CODES,
        category_labels=CATEGORY_LABELS,
        known_employees=known_employees,
        weekday_labels=weekday_labels,
        week_deadlines=week_deadlines,
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
    db.commit()
    if added:
        flash(f"{item_date} のメニューを登録しました。", "success")
    else:
        flash("メニュー名を1つ以上入力してください。", "error")
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

    return render_template(
        "admin_import_review.html",
        dates=dates,
        parsed=result,
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
    for d in dates:
        if not request.form.get(f"include_{d}"):
            continue
        any_added = False
        for code in CATEGORY_CODES:
            name = request.form.get(f"name_{d}_{code}", "").strip()
            if upsert_menu_item(db, d, code, name):
                any_added = True
        if any_added:
            saved_count += 1
    db.commit()
    if saved_count:
        flash(f"{saved_count}日分のメニューを登録しました。", "success")
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

    menu_item = db.execute(
        "SELECT * FROM menu_items WHERE item_date = ? AND category = ?", (order_date, category)
    ).fetchone()
    if not menu_item:
        flash("その日・区分のメニューが登録されていません。先にメニューを登録してください。", "error")
        return redirect(url_for("admin_dashboard"))

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
    db.commit()
    flash(f"{employee_name} さんの注文を登録しました。", "success")
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
