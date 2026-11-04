import os
import logging
import httpx
import asyncio
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Render-ისთვის ვებ-სერვერის გაშვება (პორტის დასაჭერად)
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

# Gemini API კონფიგურაცია
MODEL = "gemini-2.5-flash"
LIVE_KEYS = [
    "AQ.Ab8RN6KD8V0UwcFq1BxRvzIP3Q8HeJn43dh88XOZwDAchAH6hQ",
    "AQ.Ab8RN6JbyIcOg2OP1tEfeckU_R3MJzr7AwSY2bjMbcvceiK9Ww",
    "AQ.Ab8RN6K-qPqK-hv51W4YiY9JANsNtmFlWXWU9AFbjUrkcla7Xg"
]

TARGET_CHANNEL = "@ALLFELUBOTS"  # შენი არხის Username

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

async def gemini(key, prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            url,
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
        )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

async def translate_text(text):
    prompt = (
        "თარგმნე შემდეგი ტექსტი ქართულად. შეინარჩუნე სახელები, ციფრები, ჰეშთეგები, "
        "ემოჯები და ბმულები. არ დაამატო კომენტარი, დაწერე მხოლოდ თარგმანი.\n\n" + text
    )
    for key in LIVE_KEYS:
        try:
            return await gemini(key, prompt)
        except Exception as e:
            logging.error(f"Gemini error: {e}")
            continue
    return None

# სატესტო შეტყობინების გაგზავნა გაშვებისას
async def send_test_message(app):
    test_text = "🚨 TEST MESSAGE: Bitcoin price breaks $100,000 threshold as market sentiment turns heavily bullish!"
    logging.info("სატესტო შეტყობინების თარგმნა...")
    
    translated = await translate_text(test_text)
    body = translated if translated else "⚠️ თარგმანის გარეშე\n\n" + test_text
    final_text = f"🧪 **სატესტო პოსტი (Render)**\n\n{body}\n\n📡 გადათარგმნილია ავტომატურად"
    
    try:
        await app.bot.send_message(chat_id=TARGET_CHANNEL, text=final_text, parse_mode="Markdown")
        logging.info("სატესტო შეტყობინება წარმატებით გაიგზავნა არხში!")
    except Exception as e:
        logging.error(f"სატესტო შეტყობინების გაგზავნის შეცდომა: {e}")

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    text = message.text or message.caption or ""
    if not text.strip():
        return

    if "📡 გადათარგმნილია ავტომატურად" in text or "⚠️ თარგმანის გარეშე" in text:
        return

    translated = await translate_text(text)
    body = translated if translated else "⚠️ თარგმანის გარეშე\n\n" + text
    final_text = f"{body}\n\n📡 გადათარგმნილია ავტომატურად"

    await context.bot.send_message(chat_id=message.chat_id, text=final_text)

async def post_init(app):
    # სტარტზევე გააგზავნის სატესტო პოსტს
    asyncio.create_task(send_test_message(app))

if __name__ == "__main__":
    Thread(target=run_flask).start()

    TOKEN = os.getenv("BOT_TOKEN", "8959231295:AAEEs-A6bD2txWI2i_7EfcKJ_peGHlIriho")
    
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), handle_channel_post))
    
    logging.info("ბოტი წარმატებით გაეშვა...")
    app.run_polling()
