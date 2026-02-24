"""
Telegram notification client.

Uses the Bot API directly via httpx — no polling loop needed since
Aperture only sends outbound alerts (no interactive bot logic in Phase 2).
Inline [Snooze] and [Wrong Category] buttons are wired up in Phase 4.
"""
import logging

import httpx

from config import settings
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)

_API_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"

_CATEGORY_EMOJI = {
    1:  "🚨",  # Urgent Alerts
    2:  "👤",  # Direct Personal
    3:  "🏛️", # Important Group
    4:  "⏰",  # Near-term Events
    5:  "📰",  # Timed Headlines
    6:  "🛍️", # Active Deals
    7:  "📅",  # Short-term Events
    8:  "📆",  # Long-term Planning
    9:  "📖",  # General Reading
    10: "📬",  # Regular Lists
    11: "🧹",  # Cleanup Needed
    12: "🗑️", # Pure Trash
}


def _html(text: str) -> str:
    """Escape HTML special characters for Telegram's HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class TelegramNotifier:
    def __init__(self):
        self._chat_id = settings.telegram_chat_id

    async def send_alert(
        self,
        triage: TriageResult,
        sender: str,
        subject: str,
        message_id: str,
    ) -> None:
        """Send an immediate alert for Action A (categories 1–2)."""
        emoji = _CATEGORY_EMOJI.get(triage.category, "📧")
        gmail_url = f"https://mail.google.com/mail/u/0/#all/{message_id}"

        text = (
            f"{emoji} <b>{_html(triage.category_name.upper())}</b>\n\n"
            f"<b>From:</b> {_html(sender)}\n"
            f"<b>Subject:</b> {_html(subject)}\n\n"
            f"{_html(triage.summary)}"
        )

        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "📬 Open in Gmail", "url": gmail_url},
                    {"text": "💤 Snooze", "callback_data": f"snooze:{message_id}"},
                    {"text": "❌ Wrong Category", "callback_data": f"wrong:{message_id}:{triage.category}"},
                ]
            ]
        }

        await self._send_message(text, reply_markup)
        logger.info(f"Telegram alert sent: cat={triage.category}, message_id={message_id}")

    async def send_text(self, text: str) -> None:
        """Send a plain HTML message (for summaries, reminders, etc.)."""
        await self._send_message(text)

    async def _send_message(self, text: str, reply_markup: dict | None = None) -> None:
        payload: dict = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(f"{_API_BASE}/sendMessage", json=payload)

        if not response.is_success:
            logger.error(
                f"Telegram sendMessage failed: "
                f"status={response.status_code} body={response.text[:200]}"
            )
            response.raise_for_status()
