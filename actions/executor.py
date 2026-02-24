"""
Action executor — maps a TriageResult to a concrete Gmail + Telegram operation.

Action A  (cat 1–2)   ALERT       : Mark read + Telegram immediate alert
Action B  (cat 3–5)   SUMMARY     : Leave unread + enqueue for daily digest
Action C  (cat 6–9)   INBOX       : No-op (stays unread in inbox)
Action D  (cat 10)    ARCHIVE     : Mark read + remove from inbox
Action E  (cat 11)    UNSUBSCRIBE : Label 'Aperture/Unsubscribe' + archive
Action F  (cat 12)    TRASH       : Mark read + move to trash
"""
import logging

from google.cloud import firestore

from gmail.client import get_or_create_label, modify_message, trash_message
from notifications.telegram import TelegramNotifier
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)

# In-process label ID cache to avoid repeated API calls across messages
_label_cache: dict[str, str] = {}


async def execute(
    triage: TriageResult,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    gmail_service,
    db: firestore.Client,
    telegram: TelegramNotifier,
) -> None:
    action = triage.action
    logger.info(
        f"Executing action={action} | cat={triage.category} ({triage.category_name}) | "
        f"message={message_id} | subject='{subject[:60]}'"
    )

    if action == "ALERT":
        # Action A: mark as read, fire Telegram alert immediately
        modify_message(gmail_service, message_id, add_labels=[], remove_labels=["UNREAD"])
        await telegram.send_alert(triage, sender, subject, message_id)

    elif action == "SUMMARY":
        # Action B: leave unread, enqueue for the 07:30 / 17:30 digest
        _enqueue_summary(db, triage, message_id, thread_id, sender, subject)

    elif action == "INBOX":
        # Action C: do nothing — stays unread in inbox
        pass

    elif action == "ARCHIVE":
        # Action D: mark as read + remove from inbox (archive)
        modify_message(gmail_service, message_id, add_labels=[], remove_labels=["UNREAD", "INBOX"])

    elif action == "UNSUBSCRIBE":
        # Action E: apply label + archive
        label_id = _get_label(gmail_service, "Aperture/Unsubscribe")
        modify_message(gmail_service, message_id, add_labels=[label_id], remove_labels=["INBOX"])

    elif action == "TRASH":
        # Action F: mark as read + trash
        trash_message(gmail_service, message_id)

    else:
        logger.warning(f"Unknown action '{action}' for message {message_id} — taking no action.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_label(gmail_service, name: str) -> str:
    """Cached label lookup/creation."""
    if name not in _label_cache:
        _label_cache[name] = get_or_create_label(gmail_service, name)
    return _label_cache[name]


def _enqueue_summary(
    db: firestore.Client,
    triage: TriageResult,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
) -> None:
    """Write a summary queue entry to Firestore for the daily digest job."""
    db.collection("aperture_summary_queue").add(
        {
            "message_id": message_id,
            "thread_id": thread_id,
            "category": triage.category,
            "category_name": triage.category_name,
            "summary": triage.summary,
            "sender": sender,
            "subject": subject,
            "enqueued_at": firestore.SERVER_TIMESTAMP,
            "dispatched": False,  # flipped to True after digest is sent
        }
    )
    logger.debug(f"Enqueued summary: message={message_id} cat={triage.category}")
