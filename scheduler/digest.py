"""
Daily digest scheduler.

Drains all undispatched items from aperture_summary_queue,
groups them by category, and sends a single Telegram message.
Runs at 07:30 and 17:30 (triggered via POST /internal/digest).
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from google.cloud import firestore
from googleapiclient.errors import HttpError

from gmail.client import build_gmail_service
from notifications.telegram import TelegramNotifier
from triage.schemas import CATEGORY_NAMES
from config import settings

logger = logging.getLogger(__name__)

_CATEGORY_EMOJI = {
    3: "🏛️",
    4: "⏰",
    5: "📰",
}

# Maximum items shown per category before truncating
_MAX_PER_CATEGORY = 10


async def send_digest(db: firestore.Client, telegram: TelegramNotifier) -> int:
    """
    Fetch undispatched summary items, send a grouped Telegram digest,
    then mark all items as dispatched.
    Returns the number of items included.
    """
    # Fetch all undispatched items — filter in Python to avoid needing
    # a composite Firestore index (dispatched + enqueued_at)
    all_docs = list(db.collection("aperture_summary_queue").stream())
    pending = [
        (doc.reference, doc.to_dict())
        for doc in all_docs
        if not doc.to_dict().get("dispatched", False)
    ]

    if not pending:
        logger.info("Digest: queue is empty, nothing to send.")
        return 0

    # Filter to emails still in the inbox (not archived or deleted).
    # Items that no longer qualify are still marked dispatched so they don't
    # re-appear in future digests.
    gmail = build_gmail_service(db)
    inbox_pending = []
    stale_refs = []
    for ref, item in pending:
        msg_id = item.get("message_id")
        try:
            msg = gmail.users().messages().get(
                userId="me", id=msg_id, format="minimal"
            ).execute()
            if "INBOX" in msg.get("labelIds", []):
                inbox_pending.append((ref, item))
            else:
                logger.info(f"Digest: skipping {msg_id} — no longer in inbox.")
                stale_refs.append(ref)
        except HttpError as e:
            if e.resp.status == 404:
                logger.info(f"Digest: skipping {msg_id} — message deleted.")
                stale_refs.append(ref)
            else:
                logger.warning(f"Digest: Gmail API error for {msg_id}: {e}")
                inbox_pending.append((ref, item))  # include on unexpected error
    pending = inbox_pending

    if not pending:
        logger.info("Digest: all queued items have been archived/deleted, nothing to send.")
        # Still mark stale items dispatched so they don't recur
        if stale_refs:
            batch = db.batch()
            for ref in stale_refs:
                batch.update(ref, {"dispatched": True, "dispatched_at": firestore.SERVER_TIMESTAMP})
            batch.commit()
        return 0

    # Sort by enqueue time (oldest first)
    pending.sort(key=lambda x: x[1].get("enqueued_at") or datetime.min)

    # Group by category
    by_category: dict[int, list[dict]] = {}
    for _, item in pending:
        cat = item.get("category", 9)
        by_category.setdefault(cat, []).append(item)

    # Build Telegram message
    now = datetime.now(ZoneInfo(settings.timezone))
    time_str = now.strftime("%I:%M %p %Z").lstrip("0")
    total = len(pending)

    lines = [f"📋 <b>Email Digest — {time_str}</b>\n"]

    for cat in sorted(by_category.keys()):
        items = by_category[cat]
        emoji = _CATEGORY_EMOJI.get(cat, "📧")
        cat_name = CATEGORY_NAMES.get(cat, f"Category {cat}")
        lines.append(f"{emoji} <b>{cat_name}</b> ({len(items)})")

        for item in items[:_MAX_PER_CATEGORY]:
            subject = item.get("subject", "(no subject)")[:65]
            sender = item.get("sender", "")
            # Strip display name cruft, keep it short
            sender_short = sender.split("<")[0].strip()[:30]
            lines.append(f"  • {subject}")

        if len(items) > _MAX_PER_CATEGORY:
            lines.append(f"  <i>… and {len(items) - _MAX_PER_CATEGORY} more</i>")

        lines.append("")

    lines.append(f"<i>{total} email{'s' if total != 1 else ''} waiting in your inbox.</i>")

    await telegram.send_text("\n".join(lines))

    # Mark all dispatched in a single batch write (inbox items + stale items)
    batch = db.batch()
    for ref, _ in pending:
        batch.update(ref, {"dispatched": True, "dispatched_at": firestore.SERVER_TIMESTAMP})
    for ref in stale_refs:
        batch.update(ref, {"dispatched": True, "dispatched_at": firestore.SERVER_TIMESTAMP})
    batch.commit()

    logger.info(f"Digest sent: {total} items across {len(by_category)} categories.")
    return total


async def send_archive_digest(db: firestore.Client, telegram: TelegramNotifier) -> int:
    """
    Fetch undispatched archive items (ARCHIVE + UNSUBSCRIBE actions), send a grouped
    Telegram morning digest, then mark all items as dispatched.
    Returns the number of items included.
    """
    all_docs = list(db.collection("aperture_archive_queue").stream())
    pending = [
        (doc.reference, doc.to_dict())
        for doc in all_docs
        if not doc.to_dict().get("dispatched", False)
    ]

    if not pending:
        logger.info("Archive digest: queue is empty, nothing to send.")
        return 0

    # Sort by enqueue time (oldest first)
    pending.sort(key=lambda x: x[1].get("enqueued_at") or datetime.min)

    # Group by category
    by_category: dict[int, list[dict]] = {}
    for _, item in pending:
        cat = item.get("category", 10)
        by_category.setdefault(cat, []).append(item)

    # Build Telegram message
    now = datetime.now(ZoneInfo(settings.timezone))
    time_str = now.strftime("%I:%M %p %Z").lstrip("0")
    total = len(pending)

    lines = [f"🗄️ <b>Morning Digest — {time_str}</b>\n"]

    for cat in sorted(by_category.keys()):
        items = by_category[cat]
        emoji = _CATEGORY_EMOJI.get(cat, "📦")
        cat_name = CATEGORY_NAMES.get(cat, f"Category {cat}")
        lines.append(f"{emoji} <b>{cat_name}</b> ({len(items)})")

        for item in items[:_MAX_PER_CATEGORY]:
            subject = item.get("subject", "(no subject)")[:65]
            lines.append(f"  • {subject}")

        if len(items) > _MAX_PER_CATEGORY:
            lines.append(f"  <i>… and {len(items) - _MAX_PER_CATEGORY} more</i>")

        lines.append("")

    lines.append(f"<i>{total} email{'s' if total != 1 else ''} auto-archived since the last digest.</i>")

    await telegram.send_text("\n".join(lines))

    # Mark all dispatched
    batch = db.batch()
    for ref, _ in pending:
        batch.update(ref, {"dispatched": True, "dispatched_at": firestore.SERVER_TIMESTAMP})
    batch.commit()

    logger.info(f"Archive digest sent: {total} items across {len(by_category)} categories.")
    return total
