# -*- coding: utf-8 -*-
"""
ایجنت‌یار - ربات تلگرامی نوبت‌دهی (نسخه پکیج اول)

نصب:   pip install "python-telegram-bot[job-queue]>=21" jdatetime tzdata
اجرا:  BOT_TOKEN=xxx ADMIN_IDS=111,222 python booking_bot.py
"""
import html
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

import jdatetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo
from telegram import (
    InlineKeyboardButton as IB,
    InlineKeyboardMarkup as IM,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ============================== تنظیمات ==============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")          # توکن از BotFather
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}  # ادمین‌های دائمی (شما)
DEVELOPER_CONTACT = os.getenv("DEVELOPER_CONTACT", "@your_id")         # آیدی پشتیبانی که به مدیر نشان داده می‌شود
DB_PATH = os.getenv("DB_PATH", "bookings.db")
STATE_PATH = os.getenv("STATE_PATH", "state.pkl")
PORT = int(os.getenv("PORT", "8000"))                                   # پورتی که هاست می‌خواهد (باید با پنل یکی باشد)
TZ = ZoneInfo("Asia/Tehran")
CANCEL_LIMIT_HOURS = 3      # لغو فقط تا این تعداد ساعت مانده به نوبت
HOLD_MINUTES = 30           # مهلت ارسال فیش بعد از انتخاب ساعت (بعد از آن ساعت آزاد می‌شود)
BOOKING_DAYS_AHEAD = 14     # مشتری تا چند روز آینده می‌تواند نوبت بگیرد
REMINDER_HOURS = (24, 4)    # یادآوری چند ساعت قبل از نوبت (هم مشتری هم مدیر)؛ برای خاموش‌کردن: ()
# =====================================================================

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("booking-bot")

DAYS = ["شنبه", "یکشنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه"]  # index 0 = شنبه
MENU_BOOK, MENU_MY = "📅 رزرو نوبت", "📋 نوبت‌های من"
BACK_TXT, CANCEL_TXT = "🔙 بازگشت", "❌ لغو"
SETUP_CHAIN = ["biz_name", "biz_field", "services", "days", "hours", "card", "holder", "deposit"]
ACTIVE = "('hold','pending','confirmed')"

_TO_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_TO_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


# ============================== ابزارهای کمکی ==============================
def norm(s: str) -> str:
    return (s or "").translate(_TO_EN).strip()


def fa(s) -> str:
    return str(s).translate(_TO_FA)


def esc(s) -> str:
    return html.escape(str(s or ""))


def now() -> datetime:
    return datetime.now(TZ)


def now_s() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def day_idx(d) -> int:
    """python weekday(): Mon=0..Sun=6  ->  شنبه=0 .. جمعه=6"""
    return (d.weekday() + 2) % 7


def appt_dt(date_s: str, time_s: str) -> datetime:
    return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def fmt_dt(date_s: str, time_s: str) -> str:
    d = datetime.strptime(date_s, "%Y-%m-%d").date()
    j = jdatetime.date.fromgregorian(date=d)
    return f"{DAYS[day_idx(d)]} {fa(j.strftime('%Y/%m/%d'))} ساعت {fa(time_s)}"


def fmt_card(c: str) -> str:
    return fa(" ".join(c[i:i + 4] for i in range(0, len(c), 4)))


def parse_times(text: str):
    """'8:00, 09:30 14:00' -> ['08:00','09:30','14:00'] ؛ در صورت خطا None"""
    t = norm(text)
    pat = r"(\d{1,2})[:.](\d{2})"
    rest = re.sub(pat, "", t)
    if not re.fullmatch(r"[\s,،;؛\-–]*", rest):
        return None
    out = set()
    for h, m in re.findall(pat, t):
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        out.add(f"{h:02d}:{m:02d}")
    return sorted(out) or None


# ============================== دیتابیس ==============================
conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit؛ تراکنش‌ها دستی با BEGIN
conn.row_factory = sqlite3.Row


def init_db():
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS services(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS hours(day INTEGER PRIMARY KEY, times TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, name TEXT, phone TEXT);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS appointments(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT, phone TEXT, service TEXT,
            date TEXT NOT NULL, time TEXT NOT NULL,
            status TEXT NOT NULL,          -- hold / pending / confirmed / cancelled / rejected / expired
            hold_until TEXT, created_at TEXT);
        -- قفل نوبت: هیچ‌وقت دو نوبت فعال در یک روز و ساعت وجود نخواهد داشت (حتی با باگ)
        CREATE TABLE IF NOT EXISTS reminders(
            appt_id INTEGER NOT NULL, hours INTEGER NOT NULL, PRIMARY KEY(appt_id, hours));
        CREATE UNIQUE INDEX IF NOT EXISTS uq_slot ON appointments(date, time)
            WHERE status IN ('hold','pending','confirmed');
        """
    )


def get(key, default=None):
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def put(key, value):
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def all_admin_ids() -> set:
    ids = set(ADMIN_IDS)
    owner = get("owner_id")
    if owner:
        ids.add(int(owner))
    ids |= {r["user_id"] for r in conn.execute("SELECT user_id FROM admins")}
    return ids


def is_admin(uid: int) -> bool:
    return uid in all_admin_ids()


def is_super(uid: int) -> bool:
    return uid in ADMIN_IDS or str(uid) == get("owner_id")


def expire_holds():
    conn.execute("UPDATE appointments SET status='expired' WHERE status='hold' AND hold_until<=?", (now_s(),))


def free_slots(date_s: str) -> list:
    expire_holds()
    d = datetime.strptime(date_s, "%Y-%m-%d").date()
    row = conn.execute("SELECT times FROM hours WHERE day=?", (day_idx(d),)).fetchone()
    if not row:
        return []
    taken = {r["time"] for r in conn.execute(
        f"SELECT time FROM appointments WHERE date=? AND status IN {ACTIVE}", (date_s,))}
    n = now()
    return [t for t in row["times"].split(",") if t not in taken and appt_dt(date_s, t) > n]


def bookable_dates() -> list:
    today = now().date()
    out = []
    for i in range(BOOKING_DAYS_AHEAD + 1):
        d = (today + timedelta(days=i)).isoformat()
        if free_slots(d):
            out.append(d)
    return out


def try_hold(uid, name, phone, service, date_s, time_s):
    """قفل اتمیک ساعت؛ اگر قبلاً گرفته شده باشد None برمی‌گرداند."""
    expire_holds()
    try:
        cur = conn.execute(
            "INSERT INTO appointments(user_id,name,phone,service,date,time,status,hold_until,created_at)"
            " VALUES(?,?,?,?,?,?, 'hold', ?, ?)",
            (uid, name, phone, service, date_s, time_s,
             (now() + timedelta(minutes=HOLD_MINUTES)).strftime("%Y-%m-%d %H:%M:%S"), now_s()),
        )
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def release_hold(context):
    bk = context.user_data.get("bk")
    if bk and bk.get("appt"):
        conn.execute("UPDATE appointments SET status='cancelled' WHERE id=? AND status='hold'", (bk["appt"],))
        bk["appt"] = None


def appt_text(r) -> str:
    return (f"#{r['id']} • {fmt_dt(r['date'], r['time'])}\n"
            f"👤 {esc(r['name'])} | 📞 {esc(r['phone'])}\n🛎 {esc(r['service'])}")


# ============================== ارسال پیام ==============================
async def say(context, cid, text, kb=None):
    try:
        return await context.bot.send_message(cid, text, reply_markup=kb)
    except TelegramError as e:
        log.warning("send_message to %s failed: %s", cid, e)


async def notify_admins(context, text, exclude=None):
    for aid in all_admin_ids():
        if aid != exclude:
            await say(context, aid, text)


def main_kb():
    return ReplyKeyboardMarkup([[MENU_BOOK, MENU_MY]], resize_keyboard=True)


def panel_kb():
    return IM([
        [IB("📋 نوبت‌های پیش‌رو", callback_data="a:list")],
        [IB("🛎 خدمات", callback_data="a:services"), IB("🗓 روز و ساعت کاری", callback_data="a:days")],
        [IB("💳 کارت و بیعانه", callback_data="a:pay"), IB("🏷 نام و حوزه", callback_data="a:biz")],
    ])


async def show_panel(context, cid):
    await say(context, cid, "🛠 <b>پنل مدیریت</b>", panel_kb())


async def show_home(context, cid, uid, text=None):
    text = text or f"سلام! به <b>{esc(get('biz_name'))}</b> خوش آمدید 🌿\nبرای گرفتن نوبت، دکمه زیر را بزنید."
    await say(context, cid, text, main_kb())


def reset_flows(context):
    release_hold(context)
    for k in ("bk", "chain", "ci", "edit", "sel", "hq", "hi", "nh"):
        context.user_data.pop(k, None)
    context.user_data["st"] = None


# ============================== مراحل راه‌اندازی (مدیر) ==============================
def setup_nav(ud):
    row = []
    if ud.get("ci", 0) > 0 or ud.get("edit"):
        row.append(IB("🔙 بازگشت", callback_data="s:back"))
    if ud.get("edit"):
        row.append(IB("❌ لغو", callback_data="s:cancel"))
    return row


def days_kb(ud):
    sel = set(ud.get("sel", []))
    rows, row = [], []
    for i, name in enumerate(DAYS):
        row.append(IB(("✅ " if i in sel else "") + name, callback_data=f"sd:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([IB("✔️ تایید و ادامه", callback_data="sd:ok")])
    nav = setup_nav(ud)
    if nav:
        rows.append(nav)
    return IM(rows)


async def begin_chain(context, cid, chain, edit):
    reset_flows(context)
    ud = context.user_data
    ud.update(chain=chain, ci=0, edit=edit)
    await chain_show(context, cid)


async def chain_show(context, cid):
    ud = context.user_data
    step = ud["chain"][ud["ci"]]
    ud["st"] = "s_" + step
    nav = setup_nav(ud)
    kb = IM([nav]) if nav else None

    if step == "biz_name":
        cur = get("biz_name")
        await say(context, cid,
                  "👋 به <b>ایجنت‌یار</b> خوش آمدید!\nما می‌خواهیم کار شما را ساده‌تر کنیم؛ "
                  "خودتان این ربات را برای کسب‌وکارتان آماده می‌کنید.\n\n"
                  "1️⃣ <b>نام کسب‌وکار</b> را کامل بنویسید:" + (f"\n(فعلی: {esc(cur)})" if cur else ""), kb)
    elif step == "biz_field":
        await say(context, cid, "2️⃣ <b>حوزه فعالیت</b> را بنویسید (مثلاً: آرایشگاه زنانه، مطب دندانپزشکی):", kb)
    elif step == "services":
        cur = [r["name"] for r in conn.execute("SELECT name FROM services ORDER BY id")]
        await say(context, cid,
                  "3️⃣ <b>خدماتی</b> که ارائه می‌دهید را بنویسید.\nهر خدمت در یک خط، یا با ویرگول جدا کنید. "
                  "مثال: رنگ ناخن، هایرکات" + (f"\n\nفعلی: {esc('، '.join(cur))}" if cur else ""), kb)
    elif step == "days":
        if "sel" not in ud:
            ud["sel"] = [r["day"] for r in conn.execute("SELECT day FROM hours")]
        await say(context, cid, "4️⃣ <b>روزهای کاری</b> را انتخاب کنید (از شنبه تا جمعه):", days_kb(ud))
    elif step == "hours":
        hq, hi = ud.get("hq"), ud.get("hi", 0)
        if not hq:  # حالت غیرعادی: برگرد به انتخاب روز
            ud["ci"] -= 1
            return await chain_show(context, cid)
        await say(context, cid,
                  f"5️⃣ ساعت‌های کاری <b>{DAYS[hq[hi]]}</b> را بنویسید ({fa(hi + 1)} از {fa(len(hq))})\n"
                  "فرمت ۲۴ ساعته، با ویرگول یا فاصله جدا کنید.\nمثال: <code>08:00, 09:30, 14:00</code>", kb)
    elif step == "card":
        await say(context, cid, "6️⃣ <b>شماره کارت ۱۶ رقمی</b> خود را برای دریافت بیعانه بفرستید:", kb)
    elif step == "holder":
        await say(context, cid, "7️⃣ <b>نام صاحب کارت</b> را بنویسید:", kb)
    elif step == "deposit":
        await say(context, cid, "8️⃣ <b>مبلغ بیعانه</b> هر نوبت را به <b>تومان</b> و فقط با عدد بنویسید (مثلاً 100000):", kb)


async def chain_next(context, cid):
    ud = context.user_data
    ud["ci"] += 1
    if ud["ci"] >= len(ud["chain"]):
        return await chain_finish(context, cid)
    await chain_show(context, cid)


async def chain_back(context, cid):
    ud = context.user_data
    step = ud["chain"][ud["ci"]]
    if step == "hours" and ud.get("hi", 0) > 0:
        ud["hi"] -= 1
        return await chain_show(context, cid)
    if ud["ci"] > 0:
        ud["ci"] -= 1
        return await chain_show(context, cid)
    if ud.get("edit"):
        reset_flows(context)
        await show_panel(context, cid)


async def chain_finish(context, cid):
    edit = context.user_data.get("edit")
    reset_flows(context)
    if edit:
        await say(context, cid, "✅ تغییرات ذخیره شد.")
    else:
        put("setup_done", 1)
        await say(context, cid,
                  "🎉 <b>ربات شما آماده شد!</b>\n\n"
                  "برای اینکه بتوانیم پشتیبانی و ادمین‌کردن شما را انجام دهیم، آیدی عددی زیر را به "
                  f"{esc(DEVELOPER_CONTACT)} پیام بدهید:\n<code>{cid}</code>\n\n"
                  "حالا لینک ربات را در شبکه‌های اجتماعی بگذارید. برای مدیریت، دستور /admin را بزنید.")
    await show_panel(context, cid)


async def setup_input(update, context, text):
    ud, cid = context.user_data, update.effective_chat.id
    step = ud["chain"][ud["ci"]]
    text_n = text.strip()

    async def bad(msg):
        await say(context, cid, "⚠️ " + msg)

    if step in ("biz_name", "biz_field", "holder"):
        if not (2 <= len(text_n) <= 60):
            return await bad("متن باید بین ۲ تا ۶۰ کاراکتر باشد.")
        put({"biz_name": "biz_name", "biz_field": "biz_field", "holder": "card_holder"}[step], text_n)

    elif step == "services":
        items = []
        for p in re.split(r"[\n,،;؛]+", text_n):
            p = p.strip()
            if p and p not in items:
                items.append(p)
        if not items or any(len(i) > 40 for i in items) or len(items) > 30:
            return await bad("حداقل یک خدمت (حداکثر ۳۰ مورد، هرکدام تا ۴۰ کاراکتر) بنویسید.")
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM services")
            conn.executemany("INSERT INTO services(name) VALUES(?)", [(i,) for i in items])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    elif step == "hours":
        times = parse_times(text_n)
        if not times:
            return await bad("فرمت ساعت‌ها درست نیست. مثال: 08:00, 09:30, 14:00")
        ud.setdefault("nh", {})[str(ud["hq"][ud["hi"]])] = times
        ud["hi"] += 1
        if ud["hi"] < len(ud["hq"]):
            return await chain_show(context, cid)
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM hours")
            conn.executemany("INSERT INTO hours(day,times) VALUES(?,?)",
                             [(int(d), ",".join(t)) for d, t in ud["nh"].items()])
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    elif step == "card":
        digits = re.sub(r"\D", "", norm(text_n))
        if len(digits) != 16:
            return await bad("شماره کارت باید ۱۶ رقم باشد.")
        put("card_number", digits)

    elif step == "deposit":
        digits = re.sub(r"\D", "", norm(text_n))
        if not digits or int(digits) <= 0:
            return await bad("مبلغ را فقط با عدد و بزرگ‌تر از صفر بنویسید.")
        put("deposit", int(digits))

    else:  # days: با دکمه انجام می‌شود
        return await bad("لطفاً از دکمه‌های بالا استفاده کنید.")

    await chain_next(context, cid)


# ============================== مراحل رزرو (مشتری) ==============================
def bnav(ud):
    row = []
    if ud["bk"]["i"] > 0:
        row.append(IB("🔙 بازگشت", callback_data="b:back"))
    row.append(IB("❌ لغو", callback_data="b:cancel"))
    return row


def chunk(buttons, n):
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


async def book_start(context, cid, uid):
    reset_flows(context)
    u = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    steps = []
    if not (u and u["name"]):
        steps.append("name")
    if not (u and u["phone"]):
        steps.append("phone")
    steps += ["service", "date", "time", "receipt"]
    context.user_data["bk"] = {"steps": steps, "i": 0, "appt": None}
    await book_show(context, cid, uid)


async def book_show(context, cid, uid):
    ud = context.user_data
    bk = ud["bk"]
    step = bk["steps"][bk["i"]]
    ud["st"] = "b_" + step
    kb = IM([bnav(ud)])

    if step == "name":
        await say(context, cid, "✍️ <b>نام و نام خانوادگی</b> خود را وارد کنید:", kb)

    elif step == "phone":
        rows = [[KeyboardButton("📱 ارسال شماره تماس", request_contact=True)]]
        rows.append([BACK_TXT, CANCEL_TXT] if bk["i"] > 0 else [CANCEL_TXT])
        await say(context, cid, "📞 لطفاً شماره تماس تلگرام خود را با دکمه زیر ارسال کنید:",
                  ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True))

    elif step == "service":
        svc = conn.execute("SELECT id,name FROM services ORDER BY id").fetchall()
        if not svc:
            return await say(context, cid, "فعلاً خدمتی تعریف نشده است.", kb)
        btns = [IB(s["name"], callback_data=f"bs:{s['id']}") for s in svc]
        await say(context, cid, "🛎 <b>خدمت مورد نظر</b> را انتخاب کنید:", IM(chunk(btns, 2) + [bnav(ud)]))

    elif step == "date":
        dates = bookable_dates()
        if not dates:
            return await say(context, cid, "😔 در حال حاضر نوبت خالی وجود ندارد. بعداً دوباره سر بزنید.", kb)
        btns = []
        for d in dates:
            dd = datetime.strptime(d, "%Y-%m-%d").date()
            j = jdatetime.date.fromgregorian(date=dd)
            btns.append(IB(f"{DAYS[day_idx(dd)]} {fa(j.strftime('%m/%d'))}", callback_data=f"bd:{d}"))
        await say(context, cid, "🗓 <b>روز</b> را انتخاب کنید:", IM(chunk(btns, 2) + [bnav(ud)]))

    elif step == "time":
        slots = free_slots(bk["date"])
        if not slots:
            bk["i"] -= 1  # برگرد به انتخاب روز
            await say(context, cid, "این روز پر شد؛ روز دیگری انتخاب کنید.")
            return await book_show(context, cid, uid)
        btns = [IB(fa(t), callback_data=f"bt:{t}") for t in slots]
        await say(context, cid, f"⏰ <b>ساعت</b> نوبت را انتخاب کنید ({fmt_dt(bk['date'], '00:00').split(' ساعت')[0]}):",
                  IM(chunk(btns, 3) + [bnav(ud)]))

    elif step == "receipt":
        deposit = fa(f"{int(get('deposit')):,}")
        await say(
            context, cid,
            f"✅ ساعت <b>{fmt_dt(bk['date'], bk['time'])}</b> برای شما نگه داشته شد.\n\n"
            f"💳 برای قطعی‌شدن نوبت، بیعانه <b>{deposit} تومان</b> را به کارت زیر واریز کنید:\n"
            f"<code>{fmt_card(get('card_number'))}</code>\nبه نام: <b>{esc(get('card_holder'))}</b>\n\n"
            f"سپس <b>عکس فیش</b> را همین‌جا ارسال کنید.\n"
            f"⏳ این ساعت تا {fa(HOLD_MINUTES)} دقیقه برای شما رزرو است.", kb)


async def book_back(context, cid, uid):
    bk = context.user_data.get("bk")
    if not bk or bk["i"] == 0:
        return await book_cancel(context, cid, uid)
    if bk["steps"][bk["i"]] == "receipt":
        release_hold(context)
    bk["i"] -= 1
    await book_show(context, cid, uid)


async def book_cancel(context, cid, uid):
    reset_flows(context)
    await show_home(context, cid, uid, "فرایند لغو شد. هر وقت خواستید دوباره نوبت بگیرید 🌿")


async def book_next(context, cid, uid):
    context.user_data["bk"]["i"] += 1
    await book_show(context, cid, uid)


def save_user(uid, **fields):
    conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))
    for k, v in fields.items():
        conn.execute(f"UPDATE users SET {k}=? WHERE user_id=?", (v, uid))


async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, cid = update.effective_user.id, update.effective_chat.id
    if context.user_data.get("st") != "b_phone":
        return
    c = update.message.contact
    if c.user_id != uid:
        return await say(context, cid, "⚠️ فقط می‌توانید شماره تماس خودتان را ارسال کنید.")
    save_user(uid, phone=c.phone_number)
    await say(context, cid, "✅ شماره ثبت شد.", ReplyKeyboardRemove())
    await book_next(context, cid, uid)


async def on_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, cid = update.effective_user.id, update.effective_chat.id
    ud = context.user_data
    if ud.get("st") != "b_receipt" or not ud.get("bk"):
        return
    bk = ud["bk"]
    msg = update.message
    is_photo = bool(msg.photo)
    file_id = msg.photo[-1].file_id if is_photo else msg.document.file_id

    cur = conn.execute(
        "UPDATE appointments SET status='pending', hold_until=NULL "
        "WHERE id=? AND user_id=? AND status='hold' AND hold_until>?", (bk["appt"], uid, now_s()))
    if cur.rowcount == 0:
        bk["appt"] = None
        bk["i"] = bk["steps"].index("date")
        await say(context, cid, "⌛️ مهلت رزرو تمام شد یا ساعت آزاد شد. لطفاً دوباره انتخاب کنید.")
        return await book_show(context, cid, uid)

    r = conn.execute("SELECT * FROM appointments WHERE id=?", (bk["appt"],)).fetchone()
    kb = IM([[IB("✅ تایید", callback_data=f"a:ok:{r['id']}"), IB("❌ رد", callback_data=f"a:no:{r['id']}")]])
    caption = "🧾 <b>فیش بیعانه جدید</b>\n" + appt_text(r)
    for aid in all_admin_ids():
        try:
            if is_photo:
                await context.bot.send_photo(aid, file_id, caption=caption, reply_markup=kb)
            else:
                await context.bot.send_document(aid, file_id, caption=caption, reply_markup=kb)
        except TelegramError as e:
            log.warning("send receipt to admin %s failed: %s", aid, e)

    reset_flows(context)
    await show_home(context, cid, uid, "✅ فیش دریافت شد. پس از تایید مدیریت، پیام قطعی‌شدن نوبت برایتان ارسال می‌شود.")


async def my_appts(context, cid, uid):
    today = now().date().isoformat()
    rows = conn.execute(
        "SELECT * FROM appointments WHERE user_id=? AND status IN ('pending','confirmed') AND date>=? "
        "ORDER BY date,time", (uid, today)).fetchall()
    rows = [r for r in rows if appt_dt(r["date"], r["time"]) > now()]
    if not rows:
        return await say(context, cid, "نوبت فعالی ندارید.", main_kb())
    for r in rows:
        st = "⏳ در انتظار تایید مدیریت" if r["status"] == "pending" else "✅ تایید شده"
        txt = appt_text(r) + f"\n{st}"
        if can_cancel(r):
            await say(context, cid, txt, IM([[IB("❌ لغو این نوبت", callback_data=f"u:x:{r['id']}")]]))
        else:
            await say(context, cid, txt + f"\n(لغو فقط تا {fa(CANCEL_LIMIT_HOURS)} ساعت قبل از نوبت ممکن است)")


def can_cancel(r) -> bool:
    return appt_dt(r["date"], r["time"]) - now() >= timedelta(hours=CANCEL_LIMIT_HOURS)


# ============================== مدیریت: لیست نوبت‌ها ==============================
async def admin_list(context, cid):
    rows = conn.execute(
        "SELECT * FROM appointments WHERE status IN ('pending','confirmed') AND date>=? ORDER BY date,time LIMIT 30",
        (now().date().isoformat(),)).fetchall()
    rows = [r for r in rows if appt_dt(r["date"], r["time"]) > now()]
    if not rows:
        return await say(context, cid, "نوبت فعالی ثبت نشده است.")
    for r in rows:
        st = "⏳ منتظر تایید" if r["status"] == "pending" else "✅ تایید شده"
        await say(context, cid, appt_text(r) + f"\n{st}",
                  IM([[IB("❌ لغو نوبت", callback_data=f"a:x:{r['id']}")]]))


async def edit_note(q, note, keep_kb=False):
    """پیام را با یک یادداشت به‌روز می‌کند و دکمه‌ها را برمی‌دارد."""
    try:
        if q.message.photo or q.message.document:
            await q.edit_message_caption(caption=(q.message.caption_html or "") + "\n\n" + note, reply_markup=None)
        else:
            await q.edit_message_text((q.message.text_html or "") + "\n\n" + note, reply_markup=None)
    except TelegramError:
        pass


# ============================== Handlerها ==============================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    uid, cid = update.effective_user.id, update.effective_chat.id
    reset_flows(context)
    if get("owner_id") is None:      # اولین نفری که استارت می‌زند = مدیر کسب‌وکار
        put("owner_id", uid)
    if get("setup_done") != "1":
        if is_admin(uid):
            return await begin_chain(context, cid, list(SETUP_CHAIN), edit=False)
        return await say(context, cid, "این ربات هنوز آماده نیست؛ کمی بعد دوباره امتحان کنید 🙏")
    await show_home(context, cid, uid)
    if is_admin(uid):
        await show_panel(context, cid)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type == "private" and is_admin(uid) and get("setup_done") == "1":
        reset_flows(context)
        await show_panel(context, update.effective_chat.id)


async def cmd_addadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_super(uid):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("استفاده: /addadmin 123456789")
    conn.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (int(context.args[0]),))
    await update.message.reply_text("✅ ادمین اضافه شد.")


async def cmd_deladmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("استفاده: /deladmin 123456789")
    conn.execute("DELETE FROM admins WHERE user_id=?", (int(context.args[0]),))
    await update.message.reply_text("✅ ادمین حذف شد.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not update.message or not update.message.text:
        return
    uid, cid = update.effective_user.id, update.effective_chat.id
    ud = context.user_data
    text = update.message.text.strip()
    st = ud.get("st") or ""

    if st.startswith("s_") and is_admin(uid) and ud.get("chain"):
        return await setup_input(update, context, text)

    if get("setup_done") != "1":
        return await say(context, cid, "این ربات هنوز آماده نیست؛ کمی بعد دوباره امتحان کنید 🙏"
                         if not is_admin(uid) else "برای شروع راه‌اندازی /start را بزنید.")

    if text == MENU_BOOK:
        return await book_start(context, cid, uid)
    if text == MENU_MY:
        return await my_appts(context, cid, uid)

    if st.startswith("b_") and ud.get("bk"):
        if text == CANCEL_TXT:
            return await book_cancel(context, cid, uid)
        if text == BACK_TXT:
            return await book_back(context, cid, uid)
        if st == "b_name":
            if not (3 <= len(text) <= 60) or text.isdigit():
                return await say(context, cid, "⚠️ لطفاً نام و نام خانوادگی معتبر وارد کنید.")
            save_user(uid, name=text)
            return await book_next(context, cid, uid)
        if st == "b_phone":
            return await say(context, cid, "⚠️ لطفاً شماره را با دکمه «ارسال شماره تماس» بفرستید.")
        if st == "b_receipt":
            return await say(context, cid, "⚠️ لطفاً <b>عکس فیش</b> را ارسال کنید.")
        return await say(context, cid, "⚠️ لطفاً از دکمه‌های زیر پیام قبلی استفاده کنید.")

    await show_home(context, cid, uid)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        alert = await route(q, context, q.data or "", q.from_user.id, q.message.chat.id)
    except Exception:
        log.exception("callback error")
        alert = "خطایی رخ داد؛ دوباره تلاش کنید."
    try:
        await q.answer(alert, show_alert=bool(alert))
    except TelegramError:
        pass


async def route(q, context, data, uid, cid):
    ud = context.user_data
    st = ud.get("st") or ""
    admin = is_admin(uid)
    parts = data.split(":")
    kind = parts[0]

    if kind in ("a", "sd", "s") and not admin:
        return "دسترسی ندارید."
    if get("setup_done") != "1" and kind not in ("sd", "s"):
        return "ربات هنوز راه‌اندازی نشده است."
    stale = "این مرحله منقضی شده؛ /start را بزنید."

    # ---------- راه‌اندازی (مدیر) ----------
    if kind == "s":
        if not ud.get("chain"):
            return stale
        if parts[1] == "back":
            await chain_back(context, cid)
        elif parts[1] == "cancel":
            reset_flows(context)
            await show_panel(context, cid)
        return None

    if kind == "sd":
        if st != "s_days" or not ud.get("chain"):
            return stale
        if parts[1] == "ok":
            sel = sorted(set(ud.get("sel", [])))
            if not sel:
                return "حداقل یک روز را انتخاب کنید."
            ud.update(hq=sel, hi=0, nh={})
            await q.edit_message_reply_markup(reply_markup=None)
            await chain_next(context, cid)
        else:
            i = int(parts[1])
            sel = set(ud.get("sel", []))
            sel ^= {i}
            ud["sel"] = sorted(sel)
            await q.edit_message_reply_markup(reply_markup=days_kb(ud))
        return None

    # ---------- پنل مدیریت ----------
    if kind == "a":
        act = parts[1]
        if act == "list":
            await admin_list(context, cid)
        elif act in ("services", "days", "pay", "biz"):
            chain = {"services": ["services"], "days": ["days", "hours"],
                     "pay": ["card", "holder", "deposit"], "biz": ["biz_name", "biz_field"]}[act]
            await begin_chain(context, cid, chain, edit=True)
        elif act in ("ok", "no"):
            aid = int(parts[2])
            new = "confirmed" if act == "ok" else "rejected"
            cur = conn.execute("UPDATE appointments SET status=? WHERE id=? AND status='pending'", (new, aid))
            if cur.rowcount == 0:
                return "این مورد قبلاً بررسی شده است."
            r = conn.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
            if act == "ok":
                await say(context, r["user_id"],
                          "🎉 <b>تبریک! نوبت شما ثبت شد.</b>\n" + appt_text(r) + "\nمنتظر حضور شما هستیم 🌿")
                await edit_note(q, f"✅ تایید شد ({esc(q.from_user.full_name)})")
            else:
                await say(context, r["user_id"],
                          "متأسفانه پرداخت شما تایید نشد و نوبت آزاد شد. برای پیگیری با مدیریت تماس بگیرید.")
                await edit_note(q, f"❌ رد شد ({esc(q.from_user.full_name)})")
        elif act == "x":
            aid = parts[2]
            await q.edit_message_reply_markup(reply_markup=IM([[
                IB("بله، لغو شود", callback_data=f"a:xy:{aid}"), IB("خیر", callback_data=f"a:xn:{aid}")]]))
        elif act == "xn":
            await q.edit_message_reply_markup(reply_markup=IM([[
                IB("❌ لغو نوبت", callback_data=f"a:x:{parts[2]}")]]))
        elif act == "xy":
            aid = int(parts[2])
            cur = conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=? AND status IN ('pending','confirmed')", (aid,))
            if cur.rowcount == 0:
                return "این نوبت قبلاً لغو یا بسته شده است."
            r = conn.execute("SELECT * FROM appointments WHERE id=?", (aid,)).fetchone()
            await say(context, r["user_id"],
                      f"❌ متأسفانه نوبت شما ({fmt_dt(r['date'], r['time'])}) توسط مدیریت لغو شد. "
                      "برای هماهنگی و پیگیری بیعانه با مدیریت تماس بگیرید.")
            await edit_note(q, "❌ لغو شد و به مشتری اطلاع داده شد.")
        return None

    # ---------- مشتری: لغو نوبت ----------
    if kind == "u":
        act, aid = parts[1], int(parts[2]) if len(parts) > 2 else 0
        r = conn.execute("SELECT * FROM appointments WHERE id=? AND user_id=?", (aid, uid)).fetchone()
        if not r or r["status"] not in ("pending", "confirmed"):
            return "این نوبت دیگر فعال نیست."
        if act == "x":
            if not can_cancel(r):
                return f"لغو فقط تا {fa(CANCEL_LIMIT_HOURS)} ساعت قبل از نوبت ممکن است."
            await q.edit_message_reply_markup(reply_markup=IM([[
                IB("بله، لغو شود", callback_data=f"u:xy:{aid}"), IB("خیر", callback_data=f"u:xn:{aid}")]]))
        elif act == "xn":
            await q.edit_message_reply_markup(reply_markup=IM([[
                IB("❌ لغو این نوبت", callback_data=f"u:x:{aid}")]]))
        elif act == "xy":
            if not can_cancel(r):
                return f"لغو فقط تا {fa(CANCEL_LIMIT_HOURS)} ساعت قبل از نوبت ممکن است."
            cur = conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=? AND user_id=? AND status IN ('pending','confirmed')",
                (aid, uid))
            if cur.rowcount == 0:
                return "این نوبت دیگر فعال نیست."
            await edit_note(q, "❌ نوبت لغو شد.")
            await say(context, cid, "نوبت شما لغو شد. بازگشت بیعانه با هماهنگی مدیریت انجام می‌شود.")
            await notify_admins(context, "❌ <b>لغو نوبت توسط مشتری</b>\n" + appt_text(r))
        return None

    # ---------- مشتری: مراحل رزرو ----------
    bk = ud.get("bk")
    if kind == "b":
        if not bk:
            return stale
        if parts[1] == "back":
            await q.edit_message_reply_markup(reply_markup=None)
            await book_back(context, cid, uid)
        elif parts[1] == "cancel":
            await q.edit_message_reply_markup(reply_markup=None)
            await book_cancel(context, cid, uid)
        return None

    if kind in ("bs", "bd", "bt"):
        want = {"bs": "b_service", "bd": "b_date", "bt": "b_time"}[kind]
        if not bk or st != want:
            return stale
        val = data.split(":", 1)[1]
        if kind == "bs":
            r = conn.execute("SELECT name FROM services WHERE id=?", (int(val),)).fetchone()
            if not r:
                return "این خدمت دیگر وجود ندارد."
            bk["service"] = r["name"]
        elif kind == "bd":
            if val not in bookable_dates():
                return "این روز دیگر نوبت خالی ندارد."
            bk["date"] = val
        else:
            if val not in free_slots(bk["date"]):
                await q.edit_message_reply_markup(reply_markup=None)
                await say(context, cid, "⚠️ این ساعت همین الان گرفته شد؛ ساعت دیگری انتخاب کنید.")
                await book_show(context, cid, uid)
                return None
            u = conn.execute("SELECT name,phone FROM users WHERE user_id=?", (uid,)).fetchone()
            release_hold(context)
            appt = try_hold(uid, u["name"], u["phone"], bk["service"], bk["date"], val)
            if not appt:
                await q.edit_message_reply_markup(reply_markup=None)
                await say(context, cid, "⚠️ این ساعت همین الان گرفته شد؛ ساعت دیگری انتخاب کنید.")
                await book_show(context, cid, uid)
                return None
            bk["time"], bk["appt"] = val, appt
        await q.edit_message_reply_markup(reply_markup=None)
        await book_next(context, cid, uid)
        return None

    return None


async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    """هر دقیقه اجرا می‌شود؛ برای نوبت‌های تاییدشده یادآوری می‌فرستد (بدون ارسال تکراری، حتی بعد از ری‌استارت)."""
    if not REMINDER_HOURS or get("setup_done") != "1":
        return
    today = now().date()
    rows = conn.execute(
        "SELECT * FROM appointments WHERE status='confirmed' AND date BETWEEN ? AND ?",
        (today.isoformat(), (today + timedelta(days=max(REMINDER_HOURS) // 24 + 2)).isoformat())).fetchall()
    for r in rows:
        rem = (appt_dt(r["date"], r["time"]) - now()).total_seconds()
        if rem <= 0:
            continue
        due = [h for h in REMINDER_HOURS if rem <= h * 3600]
        if not due:
            continue
        target = min(due)
        cur = conn.execute("INSERT OR IGNORE INTO reminders(appt_id,hours) VALUES(?,?)", (r["id"], target))
        for h in due:  # یادآوری‌های بزرگ‌تر دیگر لازم نیست (مثلاً نوبتی که ۲ ساعت مانده رزرو شده)
            conn.execute("INSERT OR IGNORE INTO reminders(appt_id,hours) VALUES(?,?)", (r["id"], h))
        if cur.rowcount == 0:
            continue
        left = f"حدود {fa(max(1, round(rem / 3600)))} ساعت" if rem >= 3600 else f"{fa(max(1, round(rem / 60)))} دقیقه"
        kb = IM([[IB("❌ لغو این نوبت", callback_data=f"u:x:{r['id']}")]]) if can_cancel(r) else None
        await say(context, r["user_id"], f"⏰ <b>یادآوری نوبت</b>\n{left} تا نوبت شما مانده است.\n" + appt_text(r), kb)
        await notify_admins(context, f"⏰ <b>یادآوری نوبت</b> ({left} دیگر)\n" + appt_text(r))


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):  # لاگ اضافه نزن
        pass


def start_health_server():
    """وب‌سرور کوچک روی PORT برای هاست‌های PaaS که باید یک پورت باز ببینند (ربات خودش polling است)."""
    server = HTTPServer(("0.0.0.0", PORT), _Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server on port %s", PORT)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


def main():
    if BOT_TOKEN.startswith("PUT_YOUR"):
        raise SystemExit("BOT_TOKEN تنظیم نشده است.")
    init_db()
    start_health_server()
    app = (Application.builder()
           .token(BOT_TOKEN)
           .persistence(PicklePersistence(filepath=STATE_PATH))
           .defaults(Defaults(parse_mode=ParseMode.HTML))
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("addadmin", cmd_addadmin))
    app.add_handler(CommandHandler("deladmin", cmd_deladmin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.CONTACT & filters.ChatType.PRIVATE, on_contact))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.IMAGE) & filters.ChatType.PRIVATE, on_media))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    if app.job_queue is None:
        log.error('JobQueue فعال نیست؛ یادآوری کار نمی‌کند. نصب: pip install "python-telegram-bot[job-queue]"')
    else:
        app.job_queue.run_repeating(reminder_job, interval=60, first=15, name="reminders")
    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
