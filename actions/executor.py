"""
Action executor — maps a TriageResult to a concrete Gmail + Telegram operation.

Action A  (cat 1–2)   ALERT       : Star + label + leave unread + Telegram immediate alert
Action B  (cat 3–5)   SUMMARY     : Label + leave unread + enqueue for daily digest
Action C  (cat 6–7,9) INBOX       : Label + mark read + leave in inbox
Action D  (cat 8,10)  ARCHIVE     : Label + mark read + remove from inbox + enqueue morning digest
Action E  (cat 11)    UNSUBSCRIBE : Label 'Aperture/Unsubscribe' + archive
Action F  (cat 12)    TRASH       : Move to trash (no label)
"""
import logging

from google.cloud import firestore

from gmail.client import get_or_create_label, get_otp_from_message, modify_message, trash_message
from notifications.telegram import TelegramNotifier
from notifications.telegram_commands import is_allowlisted
from triage.schemas import TriageResult, category_rank

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
    8:  "Aperture/Planning", # Also archived
    9:  "Aperture/Reading",
    10: "Aperture/Autoarchived", # Regular Lists
    11: "Aperture/Unsubscribe",
    # 12: Pure Trash — no label (goes straight to trash)
}

# In-process label ID cache to avoid repeated API calls across messages
_label_cache: dict[str, str] = {}

# Reverse index: Gmail label ID -> category. This is what lets a thread's own
# labels act as the record of how Aperture classified it previously.
_category_by_label_id: dict[str, int] = {}
_labels_primed = False


def _prime_label_cache(gmail_service) -> None:
    """List Gmail labels once per process and index the Aperture category ones."""
    global _labels_primed
    if _labels_primed:
        return
    try:
        existing = gmail_service.users().labels().list(userId="me").execute().get("labels", [])
    except Exception as exc:
        logger.warning(f"Could not list Gmail labels: {exc}")
        return
    by_name = {lb["name"]: lb["id"] for lb in existing}
    for cat, name in CATEGORY_LABELS.items():
        label_id = by_name.get(name)
        if label_id:
            _label_cache[name] = label_id
            _category_by_label_id[label_id] = cat
    _labels_primed = True


def get_thread_floor(gmail_service, thread_id: str) -> int | None:
    """
    Highest-ranked category Aperture has already applied anywhere in this thread,
    or None if it has never labelled it.

    Replies are triaged in isolation, so a later message can be read as routine
    even though the thread is already urgent — the "Shabbat November 7" reply
    that assigned a Torah reading was archived because it mentioned a date 20
    days out. The thread's existing label is the memory that prevents that.
    """
    _prime_label_cache(gmail_service)
    if not _category_by_label_id:
        return None
    try:
        thread = (
            gmail_service.users()
            .threads()
            .get(userId="me", id=thread_id, format="minimal")
            .execute()
        )
    except Exception as exc:
        logger.warning(f"Could not read thread {thread_id} for its floor: {exc}")
        return None

    seen = {
        _category_by_label_id[label_id]
        for message in thread.get("messages", [])
        for label_id in message.get("labelIds", [])
        if label_id in _category_by_label_id
    }
    return min(seen, key=category_rank) if seen else None


def relabel_thread_category(gmail_service, thread_id: str, category: int) -> None:
    """
    Make *category*'s label the only Aperture category label on the thread.

    The thread's label is the floor's source of truth, so a stale label left in
    place would keep pinning replies to a classification the user has already
    moved away from.
    """
    _prime_label_cache(gmail_service)
    name = CATEGORY_LABELS.get(category)
    keep = _get_label(gmail_service, name) if name else None
    _clear_other_category_labels(gmail_service, thread_id, keep, add=keep)


def _clear_other_category_labels(
    gmail_service, thread_id: str, keep_label_id: str | None, add: str | None = None
) -> None:
    """Strip every Aperture category label except *keep_label_id* from the thread."""
    _prime_label_cache(gmail_service)
    stale = [lid for lid in _category_by_label_id if lid != keep_label_id]
    if not stale and not add:
        return
    try:
        gmail_service.users().threads().modify(
            userId="me",
            id=thread_id,
            body={"addLabelIds": [add] if add else [], "removeLabelIds": stale},
        ).execute()
    except Exception as exc:
        logger.warning(f"Could not tidy category labels on thread {thread_id}: {exc}")


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
    applied_label_id = label_id  # the allowlist branch below overrides this

    if action == "ALERT":
        # Action A: star + label + leave unread + fire Telegram alert
        add = ["STARRED"]
        if label_id:
            add.append(label_id)
        modify_message(gmail_service, message_id, add_labels=add, remove_labels=[])
        otp = get_otp_from_message(gmail_service, message_id)
        await telegram.send_alert(triage, sender, subject, message_id, otp=otp)

    elif action == "SUMMARY":
        # Action B: label + leave unread + enqueue for the 07:30 / 17:30 digest
        if label_id:
            modify_message(gmail_service, message_id, add_labels=[label_id], remove_labels=[])
        _enqueue_summary(db, triage, message_id, thread_id, sender, subject)

    elif action == "INBOX":
        # Action C: label + mark read + leave in inbox
        if label_id:
            modify_message(gmail_service, message_id, add_labels=[label_id], remove_labels=["UNREAD"])

    elif action == "ARCHIVE":
        # Action D: label + mark read + remove from inbox
        add = [label_id] if label_id else []
        modify_message(gmail_service, message_id, add_labels=add, remove_labels=["UNREAD", "INBOX"])
        _enqueue_archive(db, triage, message_id, thread_id, sender, subject, action)

    elif action == "UNSUBSCRIBE":
        if is_allowlisted(db, sender):
            # Treat as cat-10 (Regular Lists): archive + enqueue for morning digest
            cat10_label = _get_label(gmail_service, CATEGORY_LABELS[10])
            applied_label_id = cat10_label
            modify_message(gmail_service, message_id, add_labels=[cat10_label], remove_labels=["UNREAD", "INBOX"])
            _enqueue_archive(db, triage.model_copy(update={"category": 10}), message_id, thread_id, sender, subject, "ARCHIVE")
            logger.info(f"Allowlisted sender '{sender}' — archived as cat-10 instead of unsubscribe")
        else:
            # Action E: label + archive
            add = [label_id] if label_id else []
            modify_message(gmail_service, message_id, add_labels=add, remove_labels=["INBOX"])

    elif action == "TRASH":
        # Action F: trash (no label)
        trash_message(gmail_service, message_id)

    else:
        logger.warning(f"Unknown action '{action}' for message {message_id} — taking no action.")

    # Leave exactly one Aperture category label on the thread so the floor read
    # on the next reply is unambiguous. Trashed messages are left alone — the
    # rest of the thread keeps whatever it had.
    if action != "TRASH":
        _clear_other_category_labels(gmail_service, thread_id, applied_label_id)

    # Always log to Firestore for the dashboard (best-effort)
    _log_triage(db, triage, message_id, thread_id, sender, subject, action)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_label(gmail_service, name: str) -> str:
    """Cached label lookup/creation, keeping the ID->category index in sync."""
    if name not in _label_cache:
        label_id = get_or_create_label(gmail_service, name)
        _label_cache[name] = label_id
        for cat, cat_name in CATEGORY_LABELS.items():
            if cat_name == name:
                _category_by_label_id[label_id] = cat
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
