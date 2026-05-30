import asyncio
import logging

from config import settings
from matter.client import MatterClient

logger = logging.getLogger(__name__)


def _format_duration(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h}h {m}m {s}s"


async def get_matter_status() -> str:
    """Fetch Matter queue stats and return a formatted Telegram message."""
    client = MatterClient(settings.matter_api_key)
    queue_items, reading_seconds = await asyncio.gather(
        client.get_queue_items(),
        client.get_reading_seconds_last_7_days(),
    )

    count = len(queue_items)
    recent = [
        item.get("title") or item.get("url", "(untitled)")
        for item in queue_items[:3]
    ]
    duration = _format_duration(reading_seconds)

    article_word = "article" if count == 1 else "articles"
    lines = [f"📚 <b>Matter Queue</b> — {count} {article_word} waiting\n"]

    if count == 0:
        lines.append("<i>Queue is empty!</i>\n")
    else:
        lines.append("<b>Recently added:</b>")
        for title in recent:
            lines.append(f"  • {title[:70]}")
        lines.append("")

    lines.append(f"⏱ Reading time (last 7 days): <b>{duration}</b>")
    return "\n".join(lines)


async def send_matter_reminder(telegram) -> None:
    if not settings.matter_api_key:
        logger.warning("MATTER_API_KEY not configured — skipping Matter reminder")
        return
    try:
        message = await get_matter_status()
        await telegram.send_text(message)
        logger.info("Matter reminder sent.")
    except Exception as exc:
        logger.error(f"Matter reminder failed: {exc}")
        raise
