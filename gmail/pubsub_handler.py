"""
Processes a Gmail Pub/Sub notification:
  1. Loads the last-known historyId from Firestore.
  2. Calls Gmail history.list() to get messages added since that point.
  3. Filters out SENT / DRAFT / SPAM and non-INBOX messages.
  4. Fetches metadata (headers + snippet) for each qualifying message.
  5. Advances the stored historyId to the notification's value.
"""
import logging

from google.cloud import firestore
from googleapiclient.errors import HttpError

from gmail.client import get_history, get_message_metadata

logger = logging.getLogger(__name__)

_COLLECTION = "aperture_config"
_WATCH_DOC = "gmail_watch"

# Labels that mean we should ignore the message entirely
_SKIP_LABELS = {"SENT", "DRAFT", "SPAM", "CHAT"}


def _get_stored_history_id(db: firestore.Client) -> str | None:
    doc = db.collection(_COLLECTION).document(_WATCH_DOC).get()
    return doc.to_dict().get("history_id") if doc.exists else None


def _update_history_id(db: firestore.Client, history_id: str) -> None:
    db.collection(_COLLECTION).document(_WATCH_DOC).update({"history_id": history_id})


def process_notification(
    notification_history_id: str,
    db: firestore.Client,
    gmail_service,
) -> list[dict]:
    """
    Returns a list of message metadata dicts for every new INBOX message
    since the last processed historyId.

    Returns an empty list when there is nothing to act on (first run,
    duplicate notification, or only non-INBOX activity).
    """
    stored_id = _get_stored_history_id(db)

    if not stored_id:
        # First run: use the notification's historyId as the new baseline.
        logger.info(
            "No stored historyId — setting baseline to "
            f"{notification_history_id} and skipping this notification."
        )
        _update_history_id(db, notification_history_id)
        return []

    if notification_history_id <= stored_id:
        logger.info(
            f"Duplicate/stale notification (notif={notification_history_id}, "
            f"stored={stored_id}). Skipping."
        )
        return []

    # ── Fetch history delta ───────────────────────────────────────────────────
    try:
        history_response = get_history(
            gmail_service,
            start_history_id=stored_id,
            history_types=["messageAdded"],
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            # historyId is too old (>30 days) — reset to notification's value.
            logger.warning(
                f"historyId {stored_id} expired (404). "
                f"Resetting to {notification_history_id}."
            )
            _update_history_id(db, notification_history_id)
        else:
            logger.error(f"Gmail history fetch failed: {exc}")
        return []

    # ── Collect qualifying message IDs ────────────────────────────────────────
    new_message_ids: list[str] = []
    seen: set[str] = set()

    for record in history_response.get("history", []):
        for added in record.get("messagesAdded", []):
            msg = added["message"]
            msg_id = msg["id"]

            if msg_id in seen:
                continue
            seen.add(msg_id)

            label_ids = set(msg.get("labelIds", []))
            if label_ids & _SKIP_LABELS:
                continue
            if "INBOX" not in label_ids:
                continue

            new_message_ids.append(msg_id)

    logger.info(
        f"History delta: stored={stored_id} → notif={notification_history_id} | "
        f"{len(new_message_ids)} new INBOX message(s)"
    )

    # Advance cursor regardless of whether we found messages
    _update_history_id(db, notification_history_id)

    if not new_message_ids:
        return []

    # ── Fetch metadata for each new message ───────────────────────────────────
    messages: list[dict] = []
    for msg_id in new_message_ids:
        try:
            metadata = get_message_metadata(gmail_service, msg_id)
            messages.append(metadata)
            logger.debug(
                f"Fetched metadata: id={msg_id} | "
                f"from='{metadata['sender']}' | subject='{metadata['subject'][:60]}'"
            )
        except HttpError as exc:
            logger.error(f"Failed to fetch metadata for message {msg_id}: {exc}")

    return messages
