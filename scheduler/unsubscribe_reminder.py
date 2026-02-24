"""
Weekly unsubscribe reminder (Sundays at 10:00).

Queries Gmail for all messages labeled 'Aperture/Unsubscribe',
collects unique senders, and sends a Telegram summary with a direct Gmail link.
"""
import logging

from google.cloud import firestore

from notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

_LABEL_NAME = "Aperture/Unsubscribe"
_MAX_SENDERS = 20
_GMAIL_LABEL_URL = (
    "https://mail.google.com/mail/u/0/#label/Aperture%2FUnsubscribe"
)


async def send_unsubscribe_reminder(
    db: firestore.Client,
    gmail_service,
    telegram: TelegramNotifier,
) -> int:
    """
    Send a Telegram reminder listing senders in the Aperture/Unsubscribe label.
    Returns the approximate total message count (0 if nothing to do).
    """
    # ── Find the label ID ─────────────────────────────────────────────────────
    all_labels = (
        gmail_service.users().labels().list(userId="me").execute().get("labels", [])
    )
    label_id = next(
        (lb["id"] for lb in all_labels if lb["name"] == _LABEL_NAME), None
    )

    if not label_id:
        logger.info("Unsubscribe reminder: label '%s' does not exist yet.", _LABEL_NAME)
        return 0

    # ── List messages in the label ────────────────────────────────────────────
    response = (
        gmail_service.users()
        .messages()
        .list(userId="me", labelIds=[label_id], maxResults=50)
        .execute()
    )
    message_stubs = response.get("messages", [])
    total = response.get("resultSizeEstimate", len(message_stubs))

    if not message_stubs:
        logger.info("Unsubscribe reminder: label exists but has no messages.")
        return 0

    # ── Collect unique sender display names ───────────────────────────────────
    senders: set[str] = set()
    for stub in message_stubs[:_MAX_SENDERS]:
        msg = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=stub["id"], format="metadata", metadataHeaders=["From"])
            .execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        raw_from = headers.get("From", "Unknown")
        # Prefer display name; fall back to email address
        display = raw_from.split("<")[0].strip().strip('"') or raw_from
        senders.add(display[:55])

    # ── Build and send Telegram message ──────────────────────────────────────
    lines = [
        f"🧹 <b>Weekly Unsubscribe Reminder</b>\n",
        f"You have <b>{total}</b> email{'s' if total != 1 else ''} "
        f"pending cleanup:\n",
    ]
    for sender in sorted(senders):
        lines.append(f"  • {sender}")

    if total > _MAX_SENDERS:
        lines.append(f"  <i>… and more</i>")

    lines.append(f'\n<a href="{_GMAIL_LABEL_URL}">Review in Gmail →</a>')

    await telegram.send_text("\n".join(lines))
    logger.info("Unsubscribe reminder sent: ~%d messages.", total)
    return total
