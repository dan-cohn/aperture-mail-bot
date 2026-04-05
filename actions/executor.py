"""
Action executor — maps a TriageResult to a concrete Gmail + Telegram operation.

Action A  (cat 1–2)   ALERT       : Star + label + leave unread + Telegram immediate alert
Action B  (cat 3–5)   SUMMARY     : Label + leave unread + enqueue for daily digest
Action C  (cat 6–9)   INBOX       : Label + leave unread in inbox
Action D  (cat 10)    ARCHIVE     : Label + mark read + remove from inbox
Action E  (cat 11)    UNSUBSCRIBE : Label 'Aperture/Unsubscribe' + archive
Action F  (cat 12)    TRASH       : Move to trash (no label)
"""
import logging

from google.cloud import firestore

from gmail.client import get_or_create_label, modify_message, trash_message
from notifications.telegram import TelegramNotifier
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)

# Category → Gmail label name applied at execution time
CATEGORY_LABELS = {
    1:  "Aperture/Urgent",
    2:  "Aperture/Personal",
    3:  "Aperture/Group",
    4:  "Aperture/Events/Near-term",
    5:  "Aperture/News",
    6:  "Aperture/Deals",
    7:  "Aperture/Events/Short-term",
    8:  "Aperture/Planning",
    9:  "Aperture/Reading",
    10: "Aperture/Autoarchived", # Regular Lists
    11: "Aperture/Unsubscribe",
    # 12: Pure Trash — no label (goes straight to trash)
}

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

    # Resolve label for this category (None for cat 12)
    label_name = CATEGORY_LABELS.get(triage.category)
    label_id = _get_label(gmail_service, label_name) if label_name else None

    if action == "ALERT":
        # Action A: star + label + leave unread + fire Telegram alert
        add = ["STARRED"]
        if label_id:
            add.append(label_id)
        modify_message(gmail_service, message_id, add_labels=add, remove_labels=[])
        await telegram.send_alert(triage, sender, subject, message_id)

    elif action == "SUMMARY":
        # Action B: label + leave unread + enqueue for the 07:30 / 17:30 digest
        if label_id:
            modify_message(gmail_service, message_id, add_labels=[label_id], remove_labels=[])
        _enqueue_summary(db, triage, message_id, thread_id, sender, subject)

    elif action == "INBOX":
        # Action C: label + leave unread in inbox
        if label_id:
            modify_message(gmail_service, message_id, add_labels=[label_id], remove_labels=[])

    elif action == "ARCHIVE":
        # Action D: label + mark read + remove from inbox
        add = [label_id] if label_id else []
        modify_message(gmail_service, message_id, add_labels=add, remove_labels=["UNREAD", "INBOX"])
        _enqueue_archive(db, triage, message_id, thread_id, sender, subject, action)

    elif action == "UNSUBSCRIBE":
        # Action E: label + archive
        add = [label_id] if label_id else []
        modify_message(gmail_service, message_id, add_labels=add, remove_labels=["INBOX"])
        _enqueue_archive(db, triage, message_id, thread_id, sender, subject, action)

    elif action == "TRASH":
        # Action F: trash (no label)
        trash_message(gmail_service, message_id)

    else:
        logger.warning(f"Unknown action '{action}' for message {message_id} — taking no action.")

    # Always log to Firestore for the dashboard (best-effort)
    _log_triage(db, triage, message_id, thread_id, sender, subject, action)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_label(gmail_service, name: str) -> str:
    """Cached label lookup/creation."""
    if name not in _label_cache:
        _label_cache[name] = get_or_create_label(gmail_service, name)
    return _label_cache[name]


def _enqueue_archive(
    db: firestore.Client,
    triage: TriageResult,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    action: str,
) -> None:
    """Write an archive queue entry for the morning digest."""
    db.collection("aperture_archive_queue").add(
        {
            "message_id": message_id,
            "thread_id": thread_id,
            "category": triage.category,
            "category_name": triage.category_name,
            "summary": triage.summary,
            "sender": sender,
            "subject": subject,
            "action": action,
            "enqueued_at": firestore.SERVER_TIMESTAMP,
            "dispatched": False,
        }
    )
    logger.debug(f"Enqueued archive: message={message_id} cat={triage.category} action={action}")


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


def _log_triage(
    db: firestore.Client,
    triage: TriageResult,
    message_id: str,
    thread_id: str,
    sender: str,
    subject: str,
    action: str,
) -> None:
    """Append a triage record to aperture_triage_log for the dashboard."""
    try:
        db.collection("aperture_triage_log").add(
            {
                "message_id": message_id,
                "thread_id": thread_id,
                "sender": sender,
                "subject": subject,
                "category": triage.category,
                "category_name": triage.category_name,
                "action": action,
                "is_urgent": triage.is_urgent,
                "summary": triage.summary,
                "reasoning": triage.reasoning,
                "processed_at": firestore.SERVER_TIMESTAMP,
            }
        )
    except Exception as exc:
        logger.warning(f"Failed to write triage log for {message_id}: {exc}")
