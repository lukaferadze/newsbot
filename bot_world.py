#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram ბოტი: ფელუ თრექერი (BRICS News, Ninja News)
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

API_ID = 38550110
API_HASH = "105eca78038f6c31ec679ec8f09b8e9c"

ADMIN_CHAT = "676148895"            
TZ_NAME = "Asia/Tbilisi"
MODEL = "gemini-2.5-flash"
DATA_DIR = os.path.expanduser("~/newsbot")

BOT = {
    "name": "world",
    "title": "ფელუ თრექერი",
    "sources": ["BRICSNews", "ninjanewstr"],
    "bot_token": "8673690180:AAE48EvbZWG3DJpyw9jbl1HN7mNXy314Pqk",
    "target": "me",                 # ავტომატურად გააგზავნის შენს საკუთარ ჩატში/არხში
    "live_keys": ["AQ.Ab8RN6KD8V0UwcFq1BxRvzIP3Q8HeJn43dh88XOZwDAchAH6hQ", "AQ.Ab8RN6JbyIcOg2OP1tEfeckU_R3MJzr7AwSY2bjMbcvceiK9Ww", "AQ.Ab8RN6K-qPqK-hv51W4YiY9JANsNtmFlWXWU9AFbjUrkcla7Xg"],
    "digest_key": "AQ.Ab8RN6JPCnc4XfzphrrWB86bt3jiEXE4ThbBeBzLUq9m3TWtgw",
    "sections": (
        "1) 🌍 ქვეყნების მიხედვით: თითო ქვეყანა ცალკე ქვესათაურად (მაგ. 🇷🇺 რუსეთი), "
        "მის ქვეშ ბულეტები იმ ამბებით, რომლებიც ამ ქვეყანას ეხება.\n"
        "2) 📌 სხვა ინფორმაცია: ყველაფერი, რაც კონკრეტულ ქვეყანას არ მიეკუთვნება."
    ),
}

TZ = ZoneInfo(TZ_NAME)
MAX_BYTES = 50 * 1024 * 1024
LIVE_ROUNDS, LIVE_GAP = 5, 20
DIGEST_TRIES, DIGEST_GAP = 5, 180
DIGEST_HOUR, DIGEST_DEADLINE = 9, 14

FORMAT_RULES = (
    "ფორმატი: თითო სექციას სათაური აქვს ემოჯით, ქვეშ ბულეტები სიმბოლოთი „•“. "
    "არ გამოიყენო markdown (** # ` _). ერთი და იგივე ამბავი გააერთიანე. "
    "არაფერი გამოიგონო, გამოიყენე მხოლოდ მოცემული ტექსტი. ბმულები არ ჩაწერო. "
    "დაწერე ქართულად, მოკლედ და ნათლად."
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
os.makedirs(DATA_DIR, exist_ok=True)

def to_peer(x):
    if isinstance(x, int):
        return x
    x = str(x).strip()
    return int(x) if re.fullmatch(r"-?\d+", x) else x

def norm(t):
    t = re.sub(r"https?://\S+", "", t.lower())
    return re.sub(r"[\W_]+", "", t)

def chunks(t, n=4000):
    out = []
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        cut = cut if cut > n // 2 else n
        out.append(t[:cut])
        t = t[cut:].lstrip("\n")
    out.append(t)
    return [c for c in out if c.strip()]

def read(path):
    with open(path, "rb") as f:
        return f.read()

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

class NewsBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.name = cfg["name"]
        self.title = cfg["title"]
        self.log = logging.getLogger(self.name)
        self.bot_token = cfg["bot_token"]
        self.target = to_peer(cfg["target"])
        self.live_keys = list(cfg["live_keys"])
        self.digest_key = cfg["digest_key"]
        self.sections = cfg["sections"]
        self.sources = [to_peer(s) for s in cfg["sources"]]
        self.lock = asyncio.Lock()

        self.user = TelegramClient(os.path.join(DATA_DIR, self.name), API_ID, API_HASH)
        self.user.add_event_handler(self.on_msg, events.NewMessage(chats=self.sources))
        self.user.add_event_handler(self.on_album, events.Album(chats=self.sources))

        self.con = sqlite3.connect(os.path.join(DATA_DIR, f"{self.name}.db"), check_same_thread=False)
        self.con.executescript(
            """
CREATE TABLE IF NOT EXISTS seen(h TEXT PRIMARY KEY, ts INTEGER);
CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, source TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS digests(day TEXT PRIMARY KEY, ts INTEGER, text TEXT);
CREATE TABLE IF NOT EXISTS state(k TEXT PRIMARY KEY, v TEXT);
"""
        )
        self.con.commit()

    def missing(self):
        vals = [self.bot_token, str(self.cfg["target"]), self.digest_key] + self.live_keys
        return [v for v in vals if (not str(v).strip()) or str(v).startswith("PUT_")]

    def get_state(self, k):
        r = self.con.execute("SELECT v FROM state WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def set_state(self, k, v):
        self.con.execute("INSERT OR REPLACE INTO state(k,v) VALUES(?,?)", (k, str(v)))
        self.con.commit()

    def dedup_key(self, text, chat_id, msg_id):
        n = norm(text)[:200]
        raw = n if len(n) >= 20 else f"{chat_id}:{msg_id}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def seen_before(self, h):
        if self.con.execute("SELECT 1 FROM seen WHERE h=?", (h,)).fetchone():
            return True
        self.con.execute("INSERT INTO seen(h,ts) VALUES(?,?)", (h, int(time.time())))
        self.con.commit()
        return False

    def store_post(self, source, text):
        if text.strip():
            self.con.execute(
                "INSERT INTO posts(ts,source,text) VALUES(?,?,?)",
                (int(time.time()), source, text[:600]),
            )
            self.con.commit()

    def cleanup(self):
        now = int(time.time())
        self.con.execute("DELETE FROM seen WHERE ts<?", (now - 14 * 86400,))
        self.con.execute("DELETE FROM posts WHERE ts<?", (now - 40 * 86400,))
        self.con.execute("DELETE FROM digests WHERE ts<?", (now - 60 * 86400,))
        self.con.commit()

    async def translate_live(self, text):
        prompt = (
            "თარგმნე შემდეგი ტექსტი ქართულად. შეინარჩუნე სახელები, ციფრები, ჰეშთეგები, "
            "ემოჯები და ბმულები. არ დაამატო კომენტარი, დაწერე მხოლოდ თარგმანი.\n\n" + text
        )
        for rnd in range(1, LIVE_ROUNDS + 1):
            for i, key in enumerate(self.live_keys, 1):
                try:
                    return await gemini(key, prompt, timeout=60)
                except Exception as e:
                    self.log.warning("თარგმანი: რაუნდი %d, გასაღები %d: %s", rnd, i, type(e).__name__)
            if rnd < LIVE_ROUNDS:
                await asyncio.sleep(LIVE_GAP)
        return None

    async def tg_call(self, method, data=None, files=None):
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
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

    async def tg_text(self, text, chat=None):
        for c in chunks(text):
            await self.tg_call(
                "sendMessage",
                {"chat_id": chat or self.target, "text": c, "disable_web_page_preview": "true"},
            )

    async def tg_single(self, item, cap):
        path, kind = item
        method, field = {
            "photo": ("sendPhoto", "photo"),
            "video": ("sendVideo", "video"),
        }.get(kind, ("sendDocument", "document"))
        data = {"chat_id": self.target}
        if cap:
            data["caption"] = cap
        if kind == "video":
            data["supports_streaming"] = "true"
        await self.tg_call(method, data, {field: (os.path.basename(path), read(path))})

    async def tg_group(self, items, cap):
        kinds = {k for _, k in items}
        if "document" in kinds and len(kinds) > 1:
            for i, it in enumerate(items):
                await self.tg_single(it, cap if i == 0 else "")
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
            await self.tg_call(
                "sendMediaGroup", {"chat_id": self.target, "media": json.dumps(media)}, files
            )

    async def send_post(self, items, body):
        if not items:
            await self.tg_text(body)
            return
        cap = body if len(body) <= 1024 else ""
        if len(items) == 1:
            await self.tg_single(items[0], cap)
        else:
            await self.tg_group(items, cap)
        if body and not cap:
            await self.tg_text(body)

    async def notify_admin(self, text):
        if ADMIN_CHAT:
            try:
                await self.tg_text(text, chat=to_peer(ADMIN_CHAT))
            except Exception as e:
                self.log.warning("ადმინისთვის გაგზავნა ვერ მოხერხდა: %s", e)

    async def handle(self, msgs, chat):
        text = next((m.message for m in msgs if m.message), "") or ""
        kinds = [media_kind(m) for m in msgs]
        if not text.strip() and not any(kinds):
            return
        if self.seen_before(self.dedup_key(text, chat.id, msgs[0].id)):
            self.log.info("დუბლიკატი, გამოვტოვე")
            return

        title = getattr(chat, "title", None) or getattr(chat, "username", "") or "წყარო"
        username = getattr(chat, "username", None)
        link = f"https://t.me/{username}/{msgs[0].id}" if username else None

        body = ""
        if text.strip():
            tr = await self.translate_live(text)
            body = tr if tr else "⚠️ თარგმანის გარეშე\n\n" + text
        self.store_post(title, body or text)

        footer = f"\n\n📡 {title}"
        big = any(k and m.file and m.file.size and m.file.size > MAX_BYTES for m, k in zip(msgs, kinds))
        if big:
            body = (body + "\n\n" if body else "") + "🎬 ფაილი 50 MB-ზე დიდია."
            if link:
                body += f"\n🔗 ორიგინალი: {link}"
            await self.send_post([], body + footer)
            return

        tmp = tempfile.mkdtemp(prefix=f"nb_{self.name}_")
        try:
            items = []
            for m, k in zip(msgs, kinds):
                if k:
                    p = await m.download_media(file=tmp + "/")
                    if p:
                        items.append((p, k))
            await self.send_post(items, (body + footer) if body else footer.strip())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def safe_handle(self, msgs, chat):
        async with self.lock:
            try:
                await self.handle(msgs, chat)
            except Exception:
                self.log.exception("პოსტის დამუშავების შეცდომა")

    async def on_msg(self, event):
        if event.message.grouped_id:
            return
        await self.safe_handle([event.message], await event.get_chat())

    async def on_album(self, event):
        await self.safe_handle(list(event.messages), await event.get_chat())

    async def build_digest(self, kind):
        now = int(time.time())
        if kind == "daily":
            since = int(self.get_state("last_daily_ts") or now - 86400)
            rows = self.con.execute(
                "SELECT source,text FROM posts WHERE ts>? ORDER BY ts LIMIT 400", (since,)
            ).fetchall()
            if not rows:
                return None
            data = "\n".join(f"- [{s}] {t[:400].replace(chr(10), ' ')}" for s, t in rows)
            prompt = (
                "ქვემოთ არის ბოლო დღე-ღამის პოსტები. შეადგინე დღის დაჯამება.\n"
                f"სექციები:\n{self.sections}\n\n{FORMAT_RULES}\n\nპოსტები:\n{data}"
            )
        else:
            rows = self.con.execute(
                "SELECT day,text FROM digests WHERE ts>? ORDER BY ts", (now - 7 * 86400,)
            ).fetchall()
            if not rows:
                return None
            data = "\n\n".join(f"== {d} ==\n{t}" for d, t in rows)
            prompt = (
                "ქვემოთ არის ამ კვირის დღიური დაჯამებები. შეადგინე ერთი კვირის დაჯამება: "
                "გამოყავი მთავარი ტენდენციები და მოვლენები.\n"
                f"სექციები:\n{self.sections}\n\n{FORMAT_RULES}\n\nდაჯამებები:\n{data}"
            )
        last = None
        for attempt in range(1, DIGEST_TRIES + 1):
            try:
                return await gemini(self.digest_key, prompt, timeout=180)
            except Exception as e:
                last = e
                self.log.warning("დაჯამება (%s): მცდელობა %d: %s", kind, attempt, type(e).__name__)
                if attempt < DIGEST_TRIES:
                    await asyncio.sleep(DIGEST_GAP)
        raise RuntimeError(f"დაჯამება ვერ შედგა: {type(last).__name__}")

    async def send_digest(self, kind):
        try:
            text = await self.build_digest(kind)
        except Exception as e:
            self.log.error("%s", e)
            return False
        now = datetime.now(TZ)
        if text:
            icon, label = ("🗓", "დღის დაჯამება") if kind == "daily" else ("📊", "კვირის დაჯამება")
            try:
                await self.tg_text(f"{icon} {self.title}: {label} — {now:%d.%m.%Y}\n\n{text}")
            except Exception:
                self.log.exception("დაჯამების გაგზავნა ვერ მოხერხდა")
                return False
            if kind == "daily":
                self.con.execute(
                    "INSERT OR REPLACE INTO digests(day,ts,text) VALUES(?,?,?)",
                    (now.date().isoformat(), int(time.time()), text),
                )
                self.con.commit()
        if kind == "daily":
            self.set_state("last_daily_ts", int(time.time()))
        return True

    async def scheduler(self):
        fail_until, warned, last_cleanup = 0.0, None, None
        while True:
            try:
                now = datetime.now(TZ)
                today = now.date().isoformat()
                if last_cleanup != today:
                    self.cleanup()
                    last_cleanup = today
                if DIGEST_HOUR <= now.hour < DIGEST_DEADLINE and time.time() >= fail_until:
                    if self.get_state("last_daily") != today:
                        if await self.send_digest("daily"):
                            self.set_state("last_daily", today)
                        else:
                            fail_until = time.time() + 1800
                    if (
                        self.get_state("last_daily") == today
                        and now.weekday() == 4
                        and self.get_state("last_weekly") != today
                        and time.time() >= fail_until
                    ):
                        if await self.send_digest("weekly"):
                            self.set_state("last_weekly", today)
                        else:
                            fail_until = time.time() + 1800
                elif (
                    now.hour >= DIGEST_DEADLINE
                    and self.get_state("last_daily") != today
                    and warned != today
                ):
                    warned = today
                    await self.notify_admin(
                        f"⚠️ [{self.title}] დღევანდელი დაჯამება {DIGEST_DEADLINE}:00-მდე ვერ გაიგზავნა."
                    )
            except Exception:
                self.log.exception("scheduler-ის შეცდომა")
            await asyncio.sleep(30)

    async def run(self):
        asyncio.create_task(self.scheduler())
        await self.user.run_until_disconnected()

async def main():
    bot = NewsBot(BOT)
    miss = bot.missing()
    if miss:
        raise SystemExit(f"ჯერ კიდევ არ არის შევსებული {len(miss)} მნიშვნელობა. ჩაწერე ფაილის თავში.")
    await bot.user.start()
    me = await bot.user.get_me()
    logging.info("[%s] გაშვებულია როგორც %s. წყაროები: %s", bot.name, me.first_name, bot.sources)
    await bot.run()

if __name__ == "__main__":
    asyncio.run(main())