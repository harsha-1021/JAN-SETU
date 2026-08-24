"""
Telegram bot adapter for the Citizen Demand Intake API.

This file does NOT contain any AI logic. It only:
  1. Asks each citizen for their location ONCE (button tap or typed area
     name — no GPS knowledge required)
  2. Receives complaints (text or voice) and forwards them to the existing
     FastAPI backend (/complaints/text or /complaints/voice) using that
     citizen's real location
  3. Replies with the complaint ID as a tracking code

All translation, classification, and severity scoring already lives in
main.py — this file stays thin on purpose, so the same pattern can be
reused for a WhatsApp (Twilio) or SMS adapter later without touching
the backend at all.

Location handling is intentionally accessible: a citizen can EITHER tap
a one-time "Share my location" button, OR just type their area/village
name in their own language — both are geocoded via OpenStreetMap
Nominatim (free, no API key). This matters because requiring GPS-sharing
would quietly exclude less tech-comfortable citizens, which defeats the
whole point of the platform.

Run with:
    export TELEGRAM_BOT_TOKEN="your_token_from_botfather"
    pip install python-telegram-bot requests
    python telegram_bot.py

(Run this in a separate terminal from `uvicorn main:app`, the backend
must already be running on http://localhost:8000)
"""

import os
import requests
from typing import Dict, Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "TELEGRAM_BOT_TOKEN is not set. Get one from @BotFather on Telegram, "
        "then run: export TELEGRAM_BOT_TOKEN='your_token_here'"
    )

NOMINATIM_HEADERS = {"User-Agent": "citizen-demand-prototype/1.0"}

# In-memory per-citizen location cache: {citizen_id: {"latitude", "longitude", "region"}}
# A prototype-scale stand-in for a real user table.
USER_LOCATIONS: Dict[str, dict] = {}

LOCATION_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton("📍 Share my location", request_location=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


def geocode_place_name(place_name: str) -> Optional[dict]:
    """Forward geocode a typed area/village name into coordinates."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place_name, "format": "json", "limit": 1},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        results = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if not results:
        return None

    top = results[0]
    return {
        "latitude": float(top["lat"]),
        "longitude": float(top["lon"]),
        "region": top.get("display_name", place_name).split(",")[0],
    }


def reverse_geocode(latitude: float, longitude: float) -> str:
    """Turn shared GPS coordinates into a human-readable region name."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": latitude, "lon": longitude, "format": "json"},
            headers=NOMINATIM_HEADERS,
            timeout=10,
        )
        data = resp.json()
    except (requests.RequestException, ValueError):
        return "Unknown"

    address = data.get("address", {})
    return (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("county")
        or data.get("display_name", "Unknown")
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    citizen_id = str(update.effective_user.id)
    if citizen_id in USER_LOCATIONS:
        region = USER_LOCATIONS[citizen_id]["region"]
        await update.message.reply_text(
            f"Welcome back! You're set as {region}. Go ahead and send your complaint, "
            f"as text or voice. Send /location if you've moved."
        )
        return

    await update.message.reply_text(
        "Welcome! Before your first complaint, tell me where you are.\n\n"
        "Tap the button below to share your location — OR just type your "
        "area or village name if that's easier.",
        reply_markup=LOCATION_KEYBOARD,
    )


async def handle_location_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    citizen_id = str(update.effective_user.id)
    loc = update.message.location
    region = reverse_geocode(loc.latitude, loc.longitude)

    USER_LOCATIONS[citizen_id] = {
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "region": region,
    }

    await update.message.reply_text(
        f"Got it — {region}. Now go ahead and tell me your complaint, in text or voice.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    citizen_id = str(update.effective_user.id)
    text = update.message.text

    # First-time citizens (or anyone who typed instead of tapping the
    # button): treat this message as their area/village name, not a
    # complaint, until we have a location on file for them.
    if citizen_id not in USER_LOCATIONS:
        geocoded = geocode_place_name(text)
        if geocoded is None:
            await update.message.reply_text(
                "I couldn't find that place. Could you try a nearby town or "
                "district name instead, or tap the location button below?",
                reply_markup=LOCATION_KEYBOARD,
            )
            return

        USER_LOCATIONS[citizen_id] = geocoded
        await update.message.reply_text(
            f"Got it — {geocoded['region']}. Now go ahead and tell me your "
            f"complaint, in text or voice.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    location = USER_LOCATIONS[citizen_id]
    response = requests.post(
        f"{BACKEND_URL}/complaints/text",
        data={
            "text": text,
            "citizen_id": citizen_id,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "region": location["region"],
        },
    )

    if response.status_code == 200:
        result = response.json()
        await update.message.reply_text(
            f"Complaint received (category: {result['category']}).\n"
            f"Your tracking code is {result['tracking_code']} — save this to check status later."
        )
    else:
        await update.message.reply_text(
            "Sorry, something went wrong submitting your complaint. Please try again."
        )


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    citizen_id = str(update.effective_user.id)

    if citizen_id not in USER_LOCATIONS:
        await update.message.reply_text(
            "Before your first complaint, please tell me where you are — "
            "tap the button below or type your area name.",
            reply_markup=LOCATION_KEYBOARD,
        )
        return

    location = USER_LOCATIONS[citizen_id]
    voice_file = await update.message.voice.get_file()
    audio_bytes = await voice_file.download_as_bytearray()

    response = requests.post(
        f"{BACKEND_URL}/complaints/voice",
        files={"audio": ("voice.ogg", bytes(audio_bytes), "audio/ogg")},
        data={
            "citizen_id": citizen_id,
            "latitude": location["latitude"],
            "longitude": location["longitude"],
            "region": location["region"],
        },
    )

    if response.status_code == 200:
        result = response.json()
        await update.message.reply_text(
            f"Voice complaint received (category: {result['category']}).\n"
            f"Transcribed: \"{result['translated_text']}\"\n"
            f"Your tracking code is {result['tracking_code']} — save this to check status later."
        )
    else:
        await update.message.reply_text(
            "Sorry, something went wrong processing your voice message. Please try again."
        )


if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("location", start))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_share))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    print("Telegram bot is running. Press Ctrl+C to stop.")
    app.run_polling()
