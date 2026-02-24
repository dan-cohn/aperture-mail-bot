"""
Gmail API push-notification management.

Gmail watch() expires after 7 days.  Run setup_watch.py weekly
(or wire it into a Cloud Scheduler job that hits POST /internal/renew-watch).
"""
import logging
from datetime import datetime, timezone

from google.cloud import firestore
from googleapiclient.discovery import build

from auth.gmail_auth import get_valid_credentials
from config import settings

logger = logging.getLogger(__name__)

_COLLECTION = "aperture_config"
_WATCH_DOC = "gmail_watch"


def setup_watch(db: firestore.Client) -> dict:
    """
    Register Gmail push notifications to the Pub/Sub topic defined in config.
    Stores historyId + expiration in Firestore for renewal tracking.

    Prerequisites
    -------------
    * The Pub/Sub topic must already exist.
    * gmail-api-push@system.gserviceaccount.com must have the Pub/Sub Publisher
      role on that topic (one-time IAM grant — see setup guide).
    """
    creds = get_valid_credentials(db)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    body = {
        "topicName": settings.pubsub_topic_path,
        "labelIds": ["INBOX"],
        "labelFilterAction": "include",
    }

    response = service.users().watch(userId="me", body=body).execute()

    expiry_ms = int(response["expiration"])
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)

    logger.info(
        f"Gmail watch registered | historyId={response['historyId']} "
        f"| expires={expiry_dt.isoformat()}"
    )

    db.collection(_COLLECTION).document(_WATCH_DOC).set(
        {
            "history_id": response["historyId"],
            "expiration_ms": expiry_ms,
            "expiration_iso": expiry_dt.isoformat(),
            "topic_name": settings.pubsub_topic_path,
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    return response


def stop_watch(db: firestore.Client) -> None:
    """Unregister Gmail push notifications and clear Firestore state."""
    creds = get_valid_credentials(db)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    service.users().stop(userId="me").execute()
    db.collection(_COLLECTION).document(_WATCH_DOC).delete()
    logger.info("Gmail watch stopped.")


def get_watch_state(db: firestore.Client) -> dict | None:
    """Return the current watch metadata from Firestore, or None."""
    doc = db.collection(_COLLECTION).document(_WATCH_DOC).get()
    return doc.to_dict() if doc.exists else None
