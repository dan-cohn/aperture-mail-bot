#!/usr/bin/env python3
"""
Local integration test — no emails are modified.

What it does:
  1. Fetches the N most recent INBOX messages from Gmail (read-only).
  2. Runs each through Gemini triage and prints the result.
  3. Sends a sample Telegram alert for the first cat 1–2 hit found
     (or a synthetic one if none are found), so you can verify the bot works.

Usage:
    python scripts/test_local.py          # test 10 most recent messages
    python scripts/test_local.py --count 20
    python scripts/test_local.py --telegram-only
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import firestore
from googleapiclient.discovery import build

from auth.gmail_auth import get_valid_credentials
from config import settings
from notifications.telegram import TelegramNotifier
from triage.llm_client import get_triage_client
from triage.schemas import ACTION_MAP, CATEGORY_NAMES, TriageResult

# ── ANSI colours for terminal output ─────────────────────────────────────────
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
GREY   = "\033[90m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

ACTION_COLOUR = {
    "ALERT":       RED,
    "SUMMARY":     YELLOW,
    "INBOX":       GREEN,
    "ARCHIVE":     CYAN,
    "UNSUBSCRIBE": GREY,
    "TRASH":       GREY,
}


def fetch_inbox_messages(db: firestore.Client, count: int) -> list[dict]:
    """Return metadata for the *count* most recent INBOX messages."""
    creds = get_valid_credentials(db)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    # List message IDs
    list_response = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=count)
        .execute()
    )
    message_stubs = list_response.get("messages", [])

    messages = []
    for stub in message_stubs:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=stub["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        messages.append(
            {
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "sender": headers.get("From", "Unknown"),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            }
        )
    return messages


def print_result(index: int, msg: dict, triage: TriageResult) -> None:
    action = triage.action
    colour = ACTION_COLOUR.get(action, RESET)
    print(f"\n{BOLD}── Message {index} ──────────────────────────────────────{RESET}")
    print(f"  From    : {msg['sender'][:80]}")
    print(f"  Subject : {msg['subject'][:80]}")
    print(f"  Date    : {msg['date']}")
    print(f"  Snippet : {msg['snippet'][:100]}…")
    print(
        f"  {BOLD}Category{RESET}: [{triage.category}] {triage.category_name}  "
        f"→  {colour}{BOLD}{action}{RESET}"
    )
    print(f"  Summary : {triage.summary}")
    print(f"  Reason  : {triage.reasoning}")


async def send_test_telegram(msg: dict, triage: TriageResult) -> None:
    notifier = TelegramNotifier()
    print(f"\n{BOLD}Sending Telegram alert…{RESET}")
    await notifier.send_alert(triage, msg["sender"], msg["subject"], msg["id"])
    print(f"{GREEN}Telegram alert sent! Check your bot.{RESET}")


async def send_synthetic_telegram() -> None:
    """Send a synthetic alert when no urgent messages were found."""
    notifier = TelegramNotifier()
    synthetic_triage = TriageResult(
        category=1,
        is_urgent=True,
        summary="This is a test alert from Aperture to verify your Telegram bot is working.",
        reasoning="Synthetic test message.",
        suggested_action="ALERT",
    )
    fake_msg = {
        "sender": "Aperture Test <noreply@aperture.local>",
        "subject": "🧪 Aperture Telegram Test",
        "id": "test_message_id_000",
    }
    print(f"\n{BOLD}No urgent messages found — sending synthetic Telegram alert…{RESET}")
    await notifier.send_alert(synthetic_triage, fake_msg["sender"], fake_msg["subject"], fake_msg["id"])
    print(f"{GREEN}Telegram alert sent! Check your bot.{RESET}")


async def main(count: int, telegram_only: bool) -> None:
    db = firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )

    if telegram_only:
        await send_synthetic_telegram()
        return

    print(f"{BOLD}Aperture Local Test{RESET}")
    print(f"Project : {settings.gcp_project_id}")
    print(f"LLM     : {settings.llm_provider}")
    print(f"Fetching {count} most recent INBOX messages…\n")

    messages = fetch_inbox_messages(db, count)
    if not messages:
        print("No messages found in inbox.")
        return

    triage_client = get_triage_client()
    urgent_hit: tuple[dict, TriageResult] | None = None

    for i, msg in enumerate(messages, 1):
        triage = triage_client.triage(
            sender=msg["sender"],
            subject=msg["subject"],
            snippet=msg["snippet"],
            date=msg["date"],
        )
        print_result(i, msg, triage)
        if urgent_hit is None and triage.action == "ALERT":
            urgent_hit = (msg, triage)

    # ── Summary table ─────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'─'*55}{RESET}")
    print(f"{BOLD}{'ACTION':<14} COUNT{RESET}")
    print(f"{'─'*55}")

    # Re-run? No — collect counts from loop above.
    # Quick recount by re-triaging would be wasteful; just show what we printed.
    print(f"(see per-message output above)\n")

    # ── Telegram test ─────────────────────────────────────────────────────────
    if urgent_hit:
        msg, triage = urgent_hit
        await send_test_telegram(msg, triage)
    else:
        await send_synthetic_telegram()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aperture local integration test")
    parser.add_argument("--count", type=int, default=10, help="Number of inbox messages to triage")
    parser.add_argument("--telegram-only", action="store_true", help="Only test the Telegram bot")
    args = parser.parse_args()
    asyncio.run(main(count=args.count, telegram_only=args.telegram_only))
