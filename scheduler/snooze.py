"""
Snooze processor — re-fires Telegram alerts for expired snooze entries.
Triggered by POST /internal/process-snoozes every 15 minutes via Cloud Scheduler.
"""
import logging
from datetime import datetime, timezone

from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter

from gmail.client import build_gmail_service
from notifications.telegram import TelegramNotifier
from scheduler.digest import send_archive_digest, send_digest
from scheduler.unsubscribe_reminder import send_unsubscribe_reminder
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)


async def process_snoozes(db: firestore.Client, telegram: TelegramNotifier) -> int:
    """
    Re-send alerts for all snooze entries whose snooze_until time has passed.
    Returns the number of alerts re-fired.
    """
    now = datetime.now(timezone.utc)
    docs = list(db.collection("aperture_snoozes").where(filter=FieldFilter("sent", "==", False)).stream())

    if not docs:
        return 0

    count = 0
    for doc in docs:
        data = doc.to_dict()
        snooze_until = data.get("snooze_until")
        if not snooze_until:
            continue

        # Firestore Timestamps have .replace(); plain datetimes might not have tzinfo
        if hasattr(snooze_until, "replace"):
            snooze_until = snooze_until.replace(tzinfo=timezone.utc)

        if snooze_until > now:
            continue  # still sleeping

        if data.get("type") == "digest":
            digest_type = data.get("digest_type", "evening")
            text = data.get("text")
            if text:
                # Re-send the exact message that was snoozed. Rebuilding would be
                # wrong for the morning/evening digests: their items were marked
                # dispatched when first sent, so a rebuild shows only newly-arrived
                # mail — or nothing at all — silently losing what was deferred.
                await telegram.send_digest_with_snooze(text, digest_type)
            elif digest_type == "morning":
                await send_archive_digest(db, telegram)
            elif digest_type == "unsubscribe":
                # No captured text: this one rebuilds from the Gmail label, which
                # is self-filtering, so a fresh build is both correct and current.
                await send_unsubscribe_reminder(db, build_gmail_service(db), telegram)
            else:
                await send_digest(db, telegram)
            logger.info(
                f"Digest snooze re-fired: type={digest_type} "
                f"({'verbatim' if text else 'rebuilt'})"
            )
        else:
            # Reconstruct a minimal TriageResult for the alert
            triage = TriageResult(
                category=data.get("category", 1),
                is_urgent=True,
                summary=data.get("summary", "(No summary available)"),
                reasoning="Snooze period expired.",
                suggested_action="ALERT",
            )
            subject = f"💤 [Snoozed] {data.get('subject', '')}"
            await telegram.send_alert(
                triage=triage,
                sender=data.get("sender", ""),
                subject=subject,
                message_id=data.get("message_id", ""),
            )
            logger.info(f"Snooze re-fired: message_id={data.get('message_id')}")

        doc.reference.update({
            "sent":    True,
            "sent_at": firestore.SERVER_TIMESTAMP,
        })
        count += 1

    if count:
        logger.info(f"Processed {count} expired snooze(s).")
    return count
