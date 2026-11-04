#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram news bot:
  წყარო არხები -> ქართული თარგმანი (Gemini) -> შენი არხი + დღის/კვირის დაჯამება.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from telethon import TelegramClient, events

# ==== BOT-SPECIFIC START ====
BOT_NAME = "world"
DEFAULT_SOURCES = ["BRICSNews", "NinjaNews"]  # შეამოწმე ზუსტი username-ები, ან .env-ში SOURCES=
DIGEST_TITLE = "ფელუ თრექერი"
DIGEST_SECTIONS = (
    "1) 🌍 ქვეყნების მიხედვით: თითო ქვეყანა ცალკე ქვესათაურად (მაგ. 🇷🇺 რუსეთი), "
    "მის ქვეშ ბულეტები იმ ამბებით, რომლებიც ამ ქვეყანას ეხება.\n"
    "2) 📌 სხვა ინფორმაცია: ყველაფერი, რაც კონკრეტულ ქვეყანას არ მიეკუთვნება."
)
# ==== BOT-SPECIFIC END ====

BASE_DIR = os.path.expanduser("~/newsbot")
os.makedirs(BASE_DIR, exist_ok=True)
ENV_FILE = os.path.join(BASE_DIR, f"{BOT_NAME}.env")
DB_PATH = os.path.join(BASE_DIR, f"{BOT_NAME}.db")
SESSION = os.path.join(BASE_DIR, BOT_NAME)

logging.basicConfig(
    level=logging.INFO,
    format=f"%(asctime)s [{BOT_NAME}] %(levelname)s %(message)s",
)
log = logging.getLogger(BOT_NAME)


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env(ENV_FILE)


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        raise SystemExit(f"აკლია {name} ფაილში {ENV_FILE}")
    return v


def to_peer(x):
    x = x.strip()
    return int(x) if re.fullmatch(r"-?\d+", x) else x


API_ID = int(need("API_ID"))
API_HASH = need("API_HASH")
BOT_TOKEN = need("BOT_TOKEN")
TARGET = to_peer(need("TARGET_CHAT"))
LIVE_KEYS = [k.strip() for k in need("GEMINI_KEYS").split(",") if k.strip()]  # 3 გასაღები
DIGEST_KEY = need("GEMINI_DIGEST_KEY")  # მე-4 გასაღები
ADMIN_CHAT = os.environ.get("ADMIN_CHAT", "").strip()
SOURCES = [to_peer(s) for s in os.environ.get("SOURCES", ",".join(DEFAULT_SOURCES)).split(",") if s.strip()]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Asia/Tbilisi"))

MAX_BYTES = 50 * 1024 * 1024
LIVE_ROUNDS, LIVE_GAP = 5, 20          # 5 რაუნდი, 20 წმ შუალედი
DIGEST_TRIES, DIGEST_GAP = 5, 180      # 5 მცდელობა, 3 წთ შუალედი
DIGEST_HOUR, DIGEST_DEADLINE = 9, 14   # 9:00-დან 14:00-მდე ვცდილობთ

user = TelegramClient(SESSION, API_ID, API_HASH)
lock = asyncio.Lock()

# ---------------------------------------------------------------- DB
con = sqlite3.connect(DB_PATH, check_same_thread=False)
con.executescript(
    """
CREATE TABLE IF NOT EXISTS seen(h TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS digests(day TEXT PRIMARY KEY, ts INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""
)
con.commit()


def get_state(k):
    r = con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def set_state(k, v):
    con.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)", (k, str(v)))
    con.commit()


def norm(t):
    t = re.sub(r"https?://\S+", "", t.lower())
    return re.sub(r"[\W_]+", "", t)


def dedup_key(text, chat_id, msg_id):
    n = norm(text)[:200]
    raw = n if len(n) >= 20 else f"{chat_id}:{msg_id}"
    return hashlib.sha1(raw.encode()).hexdigest()


def seen_before(h):
    if con.execute("SELECT 1 FROM seen WHERE h=?", (h,)).fetchone():
        return True
    con.execute("INSERT INTO seen(h,ts) VALUES(?,?)", (h, int(time.time())))
    con.commit()
    return False


def store_post(source, text):
    if text.strip():
        con.execute(
            "INSERT INTO posts(ts,source,text) VALUES(?,?,?)",
            (int(time.time()), source, text[:600]),
        )
        con.commit()


def cleanup():
    now = int(time.time())
    con.execute("DELETE FROM seen WHERE ts<?", (now - 14 * 86400,))
    con.execute("DELETE FROM posts WHERE ts<?", (now - 40 * 86400,))
    con.execute("DELETE FROM digests WHERE ts<?", (now - 60 * 86400,))
    con.commit()


# ---------------------------------------------------------------- Gemini
async def gemini(key, prompt, timeout=120):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
    r.raise_for_status()
    parts = r.json()["candidates"][0]["content"]["parts"]
    out = "".join(p.get("text", "") for p in parts).strip()
    if not out:
        raise ValueError("ცარიელი პასუხი")
    return out


async def translate_live(text):
    prompt = (
        "თარგმნე შემდეგი ტექსტი ქართულად. შეინარჩუნე სახელები, ციფრები, ჰეშთეგები, "
        "ემოჯები და ბმულები. არ დაამატო კომენტარი, დაწერე მხოლოდ თარგმანი.\n\n" + text
    )
    for rnd in range(1, LIVE_ROUNDS + 1):
        for i, key in enumerate(LIVE_KEYS, 1):
            try:
                return await gemini(key, prompt, timeout=60)
            except Exception as e:
                log.warning("თარგმანი: რაუნდი %d, გასაღები %d: %s", rnd, i, e)
        if rnd < LIVE_ROUNDS:
            await asyncio.sleep(LIVE_GAP)
    return None


# ---------------------------------------------------------------- Telegram Bot API
async def tg_call(method, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    for _ in range(5):
        async with httpx.AsyncClient(timeout=600) as c:
            r = await c.post(url, data=data, files=files)
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 5)
            await asyncio.sleep(wait + 1)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"{method}: {r.text[:300]}")
        return r.json()
    raise RuntimeError(f"{method}: ძალიან ბევრი 429")


def chunks(t, n=4000):
    out = []
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(t[:cut])
        t = t[cut:].lstrip("\n")
    out.append(t)
    return [c for c in out if c.strip()]


async def tg_text(text, chat=None):
    for c in chunks(text):
        await tg_call(
            "sendMessage",
            {"chat_id": chat or TARGET, "text": c, "disable_web_page_preview": "true"},
        )


def read(path):
    with open(path, "rb") as f:
        return f.read()


async def tg_single(item, cap):
    path, kind = item
    method, field = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
    }.get(kind, ("sendDocument", "document"))
    data = {"chat_id": TARGET}
    if cap:
        data["caption"] = cap
    if kind == "video":
        data["supports_streaming"] = "true"
    await tg_call(method, data, {field: (os.path.basename(path), read(path))})


async def tg_group(items, cap):
    kinds = {k for _, k in items}
    if "document" in kinds and len(kinds) > 1:  # დოკუმენტი ფოტოსთან ალბომში არ ერევა
        for i, it in enumerate(items):
            await tg_single(it, cap if i == 0 else "")
        return
    for start in range(0, len(items), 10):
        part = items[start : start + 10]
        media, files = [], {}
        for i, (path, kind) in enumerate(part):
            name = f"f{i}"
            files[name] = (os.path.basename(path), read(path))
            m = {"type": kind, "media": f"attach://{name}"}
            if start == 0 and i == 0 and cap:
                m["caption"] = cap
            media.append(m)
        await tg_call(
            "sendMediaGroup",
            {"chat_id": TARGET, "media": json.dumps(media)},
            files,
        )


async def send_post(items, body):
    if not items:
        await tg_text(body)
        return
    cap = body if len(body) <= 1024 else ""
    if len(items) == 1:
        await tg_single(items[0], cap)
    else:
        await tg_group(items, cap)
    if body and not cap:
        await tg_text(body)


async def notify_admin(text):
    if ADMIN_CHAT:
        try:
            await tg_text(text, chat=to_peer(ADMIN_CHAT))
        except Exception as e:
            log.warning("ადმინისთვის გაგზავნა ვერ მოხერხდა: %s", e)


# ---------------------------------------------------------------- პოსტების დამუშავება
def media_kind(m):
    if m.sticker:
        return None
    if m.photo:
        return "photo"
    if m.video or m.gif:
        return "video"
    if m.document:
        return "document"
    return None


async def handle(msgs, chat):
    text = next((m.message for m in msgs if m.message), "") or ""
    kinds = [media_kind(m) for m in msgs]
    if not text.strip() and not any(kinds):
        return

    if seen_before(dedup_key(text, chat.id, msgs[0].id)):
        log.info("დუბლიკატი, გამოვტოვე")
        return

    title = getattr(chat, "title", None) or getattr(chat, "username", "") or "წყარო"
    username = getattr(chat, "username", None)
    link = f"https://t.me/{username}/{msgs[0].id}" if username else None

    body = ""
    if text.strip():
        tr = await translate_live(text)
        body = tr if tr else "⚠️ თარგმანის გარეშე\n\n" + text
    store_post(title, body or text)

    big = any(
        k and m.file and m.file.size and m.file.size > MAX_BYTES
        for m, k in zip(msgs, kinds)
    )
    footer = f"\n\n📡 {title}"
    if big:
        body = (body + "\n\n" if body else "") + "🎬 ფაილი 50 MB-ზე დიდია."
        if link:
            body += f"\n🔗 ორიგინალი: {link}"
        await send_post([], body + footer)
        return

    tmp = tempfile.mkdtemp(prefix="nb_")
    try:
        items = []
        for m, k in zip(msgs, kinds):
            if k:
                p = await m.download_media(file=tmp + "/")
                if p:
                    items.append((p, k))
        await send_post(items, (body + footer) if body else footer.strip())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def safe_handle(msgs, chat):
    async with lock:
        try:
            await handle(msgs, chat)
        except Exception:
            log.exception("პოსტის დამუშავების შეცდომა")


@user.on(events.NewMessage(chats=SOURCES))
async def on_msg(event):
    if event.message.grouped_id:  # ალბომს Album ჰენდლერი მოკიდებს
        return
    await safe_handle([event.message], await event.get_chat())


@user.on(events.Album(chats=SOURCES))
async def on_album(event):
    await safe_handle(list(event.messages), await event.get_chat())


# ---------------------------------------------------------------- დაჯამება
FORMAT_RULES = (
    "ფორმატი: თითო სექციას სათაური აქვს ემოჯით, ქვეშ ბულეტები სიმბოლოთი „•“. "
    "არ გამოიყენო markdown (** # ` _). ერთი და იგივე ამბავი გააერთიანე. "
    "არაფერი გამოიგონო, გამოიყენე მხოლოდ მოცემული ტექსტი. ბმულები არ ჩაწერო. "
    "დაწერე ქართულად, მოკლედ და ნათლად."
)


async def build_digest(kind):
    now = int(time.time())
    if kind == "daily":
        since = int(get_state("last_daily_ts") or now - 86400)
        rows = con.execute(
            "SELECT source,text FROM posts WHERE ts>? ORDER BY ts LIMIT 400", (since,)
        ).fetchall()
        if not rows:
            return None
        data = "\n".join(f"- [{s}] {t[:400].replace(chr(10), ' ')}" for s, t in rows)
        prompt = (
            f"ქვემოთ არის ბოლო დღე-ღამის პოსტები. შეადგინე დღის დაჯამება.\n"
            f"სექციები:\n{DIGEST_SECTIONS}\n\n{FORMAT_RULES}\n\nპოსტები:\n{data}"
        )
    else:
        rows = con.execute(
            "SELECT day,text FROM digests WHERE ts>? ORDER BY ts", (now - 7 * 86400,)
        ).fetchall()
        if not rows:
            return None
        data = "\n\n".join(f"== {d} ==\n{t}" for d, t in rows)
        prompt = (
            f"ქვემოთ არის ამ კვირის დღიური დაჯამებები. შეადგინე ერთი კვირის დაჯამება: "
            f"გამოყავი მთავარი ტენდენციები და მოვლენები.\n"
            f"სექციები:\n{DIGEST_SECTIONS}\n\n{FORMAT_RULES}\n\nდაჯამებები:\n{data}"
        )

    last = None
    for attempt in range(1, DIGEST_TRIES + 1):
        try:
            return await gemini(DIGEST_KEY, prompt, timeout=180)
        except Exception as e:
            last = e
            log.warning("დაჯამება (%s): მცდელობა %d: %s", kind, attempt, e)
            if attempt < DIGEST_TRIES:
                await asyncio.sleep(DIGEST_GAP)
    raise RuntimeError(f"დაჯამება ვერ შედგა: {last}")


async def send_digest(kind):
    """True = დასრულდა (გაიგზავნა ან არაფერი იყო გასაგზავნი); False = ვერ მოხერხდა."""
    try:
        text = await build_digest(kind)
    except Exception as e:
        log.error("%s", e)
        return False
    now = datetime.now(TZ)
    if text:
        icon = "🗓" if kind == "daily" else "📊"
        name = "დღის დაჯამება" if kind == "daily" else "კვირის დაჯამება"
        head = f"{icon} {DIGEST_TITLE}: {name} — {now:%d.%m.%Y}\n\n"
        try:
            await tg_text(head + text)
        except Exception:
            log.exception("დაჯამების გაგზავნა ვერ მოხერხდა")
            return False
        if kind == "daily":
            con.execute(
                "INSERT OR REPLACE INTO digests(day,ts,text) VALUES(?,?,?)",
                (now.date().isoformat(), int(time.time()), text),
            )
            con.commit()
    if kind == "daily":
        set_state("last_daily_ts", int(time.time()))
    return True


async def scheduler():
    fail_until = 0.0
    warned = None
    last_cleanup = None
    while True:
        try:
            now = datetime.now(TZ)
            today = now.date().isoformat()
            if last_cleanup != today:
                cleanup()
                last_cleanup = today

            in_window = DIGEST_HOUR <= now.hour < DIGEST_DEADLINE
            if in_window and time.time() >= fail_until:
                if get_state("last_daily") != today:
                    if await send_digest("daily"):
                        set_state("last_daily", today)
                    else:
                        fail_until = time.time() + 1800
                if (
                    get_state("last_daily") == today
                    and now.weekday() == 4  # პარასკევი
                    and get_state("last_weekly") != today
                    and time.time() >= fail_until
                ):
                    if await send_digest("weekly"):
                        set_state("last_weekly", today)
                    else:
                        fail_until = time.time() + 1800
            elif now.hour >= DIGEST_DEADLINE and get_state("last_daily") != today and warned != today:
                warned = today
                await notify_admin(f"⚠️ [{BOT_NAME}] დღევანდელი დაჯამება {DIGEST_DEADLINE}:00-მდე ვერ გაიგზავნა.")
        except Exception:
            log.exception("scheduler-ის შეცდომა")
        await asyncio.sleep(30)


async def main():
    await user.start()  # პირველად ითხოვს ტელეფონს და კოდს
    me = await user.get_me()
    log.info("გაშვებულია როგორც %s. წყაროები: %s", me.first_name, SOURCES)
    asyncio.create_task(scheduler())
    await user.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
