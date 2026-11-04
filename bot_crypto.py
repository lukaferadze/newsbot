import os
import sys
import asyncio
import logging
import httpx
from datetime import datetime, time
import zoneinfo
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

import threading
from flask import Flask

app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# Flask-ის გაშვება ცალკე ნაკადში (Thread)
threading.Thread(target=run_flask, daemon=True).start()

# ---- 1. ENVIRONMENT & CONFIGURATION ----


API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TARGET_CHAT = os.environ.get("TARGET_CHAT", "")
SOURCES = [s.strip() for s in os.environ.get("SOURCES", "cointelegraph").split(",") if s.strip()]

LIVE_KEYS = [k.strip() for k in os.environ.get("GEMINI_KEYS", "").split(",") if k.strip()]
DIGEST_KEY = os.environ.get("GEMINI_DIGEST_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

TZ_NAME = os.environ.get("TZ_NAME", "Asia/Tbilisi")
ADMIN_CHAT = os.environ.get("ADMIN_CHAT", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Global caches
processed_hashes = set()
daily_posts = []
last_digest_date = None

user_client = TelegramClient('crypto_user_session', API_ID, API_HASH)
bot_client = TelegramClient('crypto_bot_session', API_ID, API_HASH)

# ---- 2. GEMINI TRANSLATION LOGIC ----
async def call_gemini(key: str, prompt: str) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=60) as client:
        res = await client.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]}
        )
    res.raise_for_status()
    data = res.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

async def translate_text(text: str) -> str:
    prompt = (
        "თარგმნე შემდეგი ტექსტი ქართულად. შეინარჩუნე სახელები, ციფრები, ჰეშთეგები, "
        "ემოჯები და ბმულები. არ დაამატო კომენტარი, დაწერე მხოლოდ თარგმანი.\n\n" + text
    )
    for round_num in range(5):
        for key in LIVE_KEYS:
            try:
                return await call_gemini(key, prompt)
            except Exception as e:
                logging.warning(f"[Crypto Live API Error] Key {key[:8]} failed: {e}")
                continue
        await asyncio.sleep(20)
    return None

async def generate_digest(posts_list: list, is_weekly: bool = False) -> str:
    combined_text = "\n---\n".join(posts_list)
    title = "კრიპტო ბაზრის კვირის მიმოხილვა" if is_weekly else "კრიპტო ბაზრის დღის მიმოხილვა"
    prompt = (
        f"შექმენი {title} ქართულ ენაზე შემდეგი სიახლეების მიხედვით.\n"
        "დააჯგუფე ინფორმაცია სექციებად: 🏢 კრიპტო კომპანიები, 📈 ბაზრის ტენდენციები, ⚖️ რეგულაციები და სხვა.\n"
        "გამოიყენე ბულეტები (•). იყავი კონკრეტული და მოკლე.\n\n" + combined_text
    )
    for attempt in range(5):
        try:
            return await call_gemini(DIGEST_KEY, prompt)
        except Exception as e:
            logging.error(f"[Crypto Digest API Error] Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(180)
    return None

# ---- 3. EVENT HANDLERS ----
@user_client.on(events.NewMessage(chats=SOURCES))
async def handle_new_post(event):
    global daily_posts
    text = event.message.message or ""
    if not text.strip():
        return

    # Duplicate filter
    msg_hash = hash(text.strip())
    if msg_hash in processed_hashes:
        return
    processed_hashes.add(msg_hash)

    # Translation
    translated = await translate_text(text)
    if translated:
        final_text = f"{translated}\n\n📡 გადათარგმნილია ავტომატურად"
    else:
        final_text = f"⚠️ თარგმანის გარეშე\n\n{text}\n\n📡 გადათარგმნილია ავტომატურად"

    daily_posts.append(final_text)

    # Media handling (>50MB check)
    media = event.message.media
    file_to_send = None
    if media:
        if isinstance(media, MessageMediaDocument) and media.document.size > 50 * 1024 * 1024:
            final_text += f"\n\n🔗 [დიდი ფაილის ორიგინალი ბმული]({event.message.message_link})"
        else:
            file_to_send = await event.message.download_media()

    try:
        await bot_client.send_message(TARGET_CHAT, final_text, file=file_to_send)
    except Exception as e:
        logging.error(f"[Crypto Post Error] Failed to send message: {e}")
    finally:
        if file_to_send and os.path.exists(file_to_send):
            os.remove(file_to_send)

# ---- 4. DAILY/WEEKLY DIGEST SCHEDULER ----
async def digest_scheduler():
    global last_digest_date, daily_posts
    tz = zoneinfo.ZoneInfo(TZ_NAME)

    while True:
        now = datetime.now(tz)
        current_date = now.date()

        if now.time() >= time(9, 0) and now.time() < time(14, 0) and last_digest_date != current_date:
            if daily_posts:
                logging.info("[Crypto Digest] Generating daily digest...")
                digest_res = await generate_digest(daily_posts, is_weekly=False)
                if digest_res:
                    await bot_client.send_message(TARGET_CHAT, f"📊 **კრიპტო დღის მიმოხილვა**\n\n{digest_res}")
                    
                    # Friday weekly digest
                    if now.weekday() == 4:
                        logging.info("[Crypto Digest] Generating weekly digest...")
                        weekly_res = await generate_digest(daily_posts, is_weekly=True)
                        if weekly_res:
                            await bot_client.send_message(TARGET_CHAT, f"🗓️ **კრიპტო კვირის მიმოხილვა**\n\n{weekly_res}")
                    
                    last_digest_date = current_date
                    daily_posts.clear()
                else:
                    if ADMIN_CHAT:
                        await bot_client.send_message(ADMIN_CHAT, "⚠️ Crypto Bot: დღის დაჯამება ვერ შეიქმნა 5 მცდელობის შემდეგ.")
            else:
                last_digest_date = current_date

        await asyncio.sleep(60)

# ---- 5. MAIN EXECUTION ----
async def main():
    await user_client.start()
    await bot_client.start(bot_token=BOT_TOKEN)
    logging.info("Crypto News Bot Started...")
    asyncio.create_task(digest_scheduler())
    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )

if __name__ == "__main__":
    asyncio.run(main())
