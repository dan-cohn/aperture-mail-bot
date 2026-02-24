"""
Telegram callback query handler.

Handles inline button presses on Aperture alert messages:

  wrong:{gmail_id}:{orig_cat}              → show 12-category picker
  correct:{gmail_id}:{orig_cat}:{new_cat}  → store correction (confirmed=False)
  snooze:{gmail_id}                        → show snooze duration picker
  snooze_for:{gmail_id}:{duration}         → store snooze (duration: 1, 4, or "morning")
  cancel:{gmail_id}                        → dismiss picker, restore original buttons
  noop                                     → silently acknowledge (disabled buttons)
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from config import settings
from triage.schemas import CATEGORY_NAMES

logger = logging.getLogger(__name__)

_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

# Short labels for the 4×3 category picker keyboard
_CAT_SHORT = {
    1:  "🚨 Urgent",
    2:  "👤 Personal",
    3:  "🏛️ Group",
    4:  "⏰ <48h",
    5:  "📰 Headlines",
    6:  "🛍️ Deals",
    7:  "📅 3-7 days",
    8:  "📆 Long-term",
    9:  "📖 Reading",
    10: "📬 Regular",
    11: "🧹 Cleanup",
    12: "🗑️ Trash",
}

# Rebuild original alert buttons (used after cancel)
_GMAIL_URL = "https://mail.google.com/mail/u/0/#all/{}"


async def handle_callback(callback_query: dict, db: firestore.Client) -> None:
    """Entry point — route a Telegram callback_query to the right handler."""
    query_id   = callback_query["id"]
    data       = callback_query.get("data", "")
    tg_message = callback_query.get("message", {})
    chat_id    = tg_message.get("chat", {}).get("id")
    tg_msg_id  = tg_message.get("message_id")  # Telegram message ID (int)

    parts  = data.split(":")
    action = parts[0]

    try:
        if action == "noop":
            await _answer(query_id)

        elif action == "wrong" and len(parts) == 3:
            gmail_id, orig_cat = parts[1], int(parts[2])
            await _show_category_picker(query_id, chat_id, tg_msg_id, gmail_id, orig_cat)

        elif action == "correct" and len(parts) == 4:
            gmail_id, orig_cat, new_cat = parts[1], int(parts[2]), int(parts[3])
            await _store_correction(query_id, chat_id, tg_msg_id, gmail_id, orig_cat, new_cat, db)

        elif action == "snooze" and len(parts) == 2:
            gmail_id = parts[1]
            await _show_snooze_picker(query_id, chat_id, tg_msg_id, gmail_id)

        elif action == "snooze_for" and len(parts) == 3:
            gmail_id, duration = parts[1], parts[2]
            await _store_snooze(query_id, chat_id, tg_msg_id, gmail_id, duration, db)

        elif action == "cancel" and len(parts) >= 2:
            gmail_id = parts[1]
            await _answer(query_id, "Cancelled.")
            await _restore_alert_buttons(chat_id, tg_msg_id, gmail_id)

        else:
            await _answer(query_id)

    except Exception as exc:
        logger.exception(f"Error handling callback '{data}': {exc}")
        await _answer(query_id, "Something went wrong.")


# ── Action handlers ───────────────────────────────────────────────────────────

async def _show_category_picker(
    query_id: str, chat_id, tg_msg_id: int, gmail_id: str, orig_cat: int
) -> None:
    """Replace the alert keyboard with a 4×3 category grid."""
    cats = list(_CAT_SHORT.items())
    keyboard = []
    for i in range(0, len(cats), 3):
        row = [
            {
                "text": f"{'✓ ' if num == orig_cat else ''}{label}",
                "callback_data": f"correct:{gmail_id}:{orig_cat}:{num}",
            }
            for num, label in cats[i : i + 3]
        ]
        keyboard.append(row)
    keyboard.append([{"text": "✕ Cancel", "callback_data": f"cancel:{gmail_id}:{orig_cat}"}])

    await _answer(query_id, "Choose the correct category:")
    await _edit_keyboard(chat_id, tg_msg_id, keyboard)


async def _store_correction(
    query_id: str,
    chat_id,
    tg_msg_id: int,
    gmail_id: str,
    orig_cat: int,
    new_cat: int,
    db: firestore.Client,
) -> None:
    """Write an unconfirmed correction to aperture_corrections."""
    if orig_cat == new_cat:
        await _answer(query_id, "That's the same category — nothing changed.")
        return

    # Look up the original email from the triage log for sender/subject/snippet
    original = _fetch_log_entry(db, gmail_id)

    db.collection("aperture_corrections").add({
        "message_id":           gmail_id,
        "sender":               original.get("sender", ""),
        "subject":              original.get("subject", ""),
        "snippet":              original.get("summary", ""),  # summary is our best snippet proxy
        "wrong_category":       orig_cat,
        "wrong_category_name":  CATEGORY_NAMES.get(orig_cat, ""),
        "correct_category":     new_cat,
        "correct_category_name": CATEGORY_NAMES.get(new_cat, ""),
        "confirmed":            False,
        "created_at":           firestore.SERVER_TIMESTAMP,
    })

    new_name = CATEGORY_NAMES.get(new_cat, str(new_cat))
    await _answer(query_id, f"Saved! Marked as [{new_cat}] {new_name}")
    await _edit_keyboard(chat_id, tg_msg_id, [[
        {"text": f"✓ Corrected → [{new_cat}] {new_name}", "callback_data": "noop"}
    ]])
    logger.info(f"Correction stored: {gmail_id} | {orig_cat}→{new_cat} | confirmed=False")


async def _show_snooze_picker(
    query_id: str, chat_id, tg_msg_id: int, gmail_id: str
) -> None:
    keyboard = [
        [
            {"text": "💤 1 hour",  "callback_data": f"snooze_for:{gmail_id}:1"},
            {"text": "💤 4 hours", "callback_data": f"snooze_for:{gmail_id}:4"},
        ],
        [{"text": "🌅 Tomorrow morning", "callback_data": f"snooze_for:{gmail_id}:morning"}],
        [{"text": "✕ Cancel", "callback_data": f"cancel:{gmail_id}"}],
    ]
    await _answer(query_id, "Choose snooze duration:")
    await _edit_keyboard(chat_id, tg_msg_id, keyboard)


async def _store_snooze(
    query_id: str,
    chat_id,
    tg_msg_id: int,
    gmail_id: str,
    duration: str,
    db: firestore.Client,
) -> None:
    """Write a snooze entry to aperture_snoozes."""
    now_utc = datetime.now(timezone.utc)
    user_tz = ZoneInfo(settings.timezone)

    if duration == "morning":
        tomorrow_local = (datetime.now(user_tz) + timedelta(days=1)).replace(
            hour=7, minute=30, second=0, microsecond=0
        )
        snooze_until = tomorrow_local.astimezone(timezone.utc)
        label = "tomorrow morning"
    else:
        hours = int(duration)
        snooze_until = now_utc + timedelta(hours=hours)
        label = f"{hours} hour{'s' if hours != 1 else ''}"

    original = _fetch_log_entry(db, gmail_id)

    db.collection("aperture_snoozes").add({
        "message_id":    gmail_id,
        "sender":        original.get("sender", ""),
        "subject":       original.get("subject", ""),
        "summary":       original.get("summary", ""),
        "category":      original.get("category", 1),
        "category_name": original.get("category_name", ""),
        "snooze_until":  snooze_until,
        "sent":          False,
        "created_at":    firestore.SERVER_TIMESTAMP,
    })

    until_str = snooze_until.strftime("%H:%M UTC")
    await _answer(query_id, f"Snoozed for {label}.")
    await _edit_keyboard(chat_id, tg_msg_id, [[
        {"text": f"💤 Snoozed — rings at {until_str}", "callback_data": "noop"}
    ]])
    logger.info(f"Snooze stored: {gmail_id} | until={snooze_until.isoformat()}")


async def _restore_alert_buttons(chat_id, tg_msg_id: int, gmail_id: str) -> None:
    """Restore the original Open / Snooze / Wrong Category buttons."""
    await _edit_keyboard(chat_id, tg_msg_id, [[
        {"text": "📬 Open in Gmail",    "url": _GMAIL_URL.format(gmail_id)},
        {"text": "💤 Snooze",           "callback_data": f"snooze:{gmail_id}"},
        {"text": "❌ Wrong Category",   "callback_data": f"wrong:{gmail_id}:0"},
    ]])


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fetch_log_entry(db: firestore.Client, gmail_id: str) -> dict:
    """Return the triage log entry for a Gmail message ID, or empty dict."""
    try:
        docs = (
            db.collection("aperture_triage_log")
            .where(filter=FieldFilter("message_id", "==", gmail_id))
            .limit(1)
            .stream()
        )
        for doc in docs:
            return doc.to_dict()
    except Exception as exc:
        logger.warning(f"Could not fetch log entry for {gmail_id}: {exc}")
    return {}


async def _answer(callback_query_id: str, text: str = "") -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{_API}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
        )


async def _edit_keyboard(chat_id, message_id: int, keyboard: list) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        await client.post(
            f"{_API}/editMessageReplyMarkup",
            json={
                "chat_id":      chat_id,
                "message_id":   message_id,
                "reply_markup": {"inline_keyboard": keyboard},
            },
        )
