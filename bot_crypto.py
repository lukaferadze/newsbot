import os
import logging
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

# Gemini API-ის კონფიგურაცია
MODEL = "gemini-2.5-flash"
LIVE_KEYS = [
    "AQ.Ab8RN6KD8V0UwcFq1BxRvzIP3Q8HeJn43dh88XOZwDAchAH6hQ",
    "AQ.Ab8RN6JbyIcOg2OP1tEfeckU_R3MJzr7AwSY2bjMbcvceiK9Ww",
    "AQ.Ab8RN6K-qPqK-hv51W4YiY9JANsNtmFlWXWU9AFbjUrkcla7Xg"
]

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
            logging.error(f"Gemini API შეცდომა: {e}")
            continue
    return None

async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return

    # ამოვიღოთ ტექსტი (თუ ფოტოა, caption-იდან)
    text = message.text or message.caption or ""
    if not text.strip():
        return

    # უსასრულო ციკლის თავიდან აცილება (თუ პოსტი უკვე თარგმნილია)
    if "📡 გადათარგმნილია ავტომატურად" in text or "⚠️ თარგმანის გარეშე" in text:
        return

    logging.info(f"ახალი პოსტი არხში: {text[:40]}...")

    translated = await translate_text(text)
    body = translated if translated else "⚠️ თარგმანის გარეშე\n\n" + text
    final_text = f"{body}\n\n📡 გადათარგმნილია ავტომატურად"

    # პასუხის გაგზავნა იმავე არხში
    await context.bot.send_message(chat_id=message.chat_id, text=final_text)

if __name__ == "__main__":
    TOKEN = os.getenv("BOT_TOKEN", "8959231295:AAEEs-A6bD2txWI2i_7EfcKJ_peGHlIriho")
    
    app = ApplicationBuilder().token(TOKEN).build()
    
    # იჭერს ტექსტურ პოსტებსაც და ფოტოებიან პოსტებსაც (caption-ით)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL & (filters.TEXT | filters.CAPTION), handle_channel_post))
    
    logging.info("ბოტი წარმატებით გაეშვა...")
    app.run_polling()
