"""
Natural language command handler for inbound Telegram messages.

Parses freeform text via Gemini and executes the inferred action.
Supported actions (extensible — add new handlers below):

  allowlist_sender   — add one or more senders/domains to the allowlist
  remove_allowlist   — remove a sender/domain from the allowlist
  list_allowlist     — show all allowlisted senders
  help               — list available commands
  query_history      — show recent triage decisions for a sender/domain
  add_correction     — add a confirmed correction rule for a sender
  list_corrections   — show pending corrections with Approve buttons
  view_prompt        — show the current learned prompt
  modify_prompt      — generatively add/update a rule in the learned prompt
  query_stats        — show triage statistics for a time period
  health_status      — show system health and queue depths
"""
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google import genai
from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from google.genai import types

from config import settings
from triage.schemas import CATEGORY_NAMES

logger = logging.getLogger(__name__)

_INTENT_PROMPT = """\
You are a command parser for Aperture, a personal email triage assistant.
Parse the user's message and return JSON with these fields (omit fields that don't apply):

  action          — one of the actions listed below (required)
  emails          — list of email addresses or domains (allowlist_sender)
  email           — single email address or domain (remove_allowlist)
  query           — sender name/email/domain to search (query_history)
  sender          — sender email or domain (add_correction)
  wrong_category  — integer 1–12, the wrong category (add_correction; 0 if unknown)
  correct_category — integer 1–12, the correct category (add_correction)
  rule            — description of the change to make (modify_prompt)
  period          — "today", "week", "month", or "all" (query_stats; default "week")
  reply           — brief plain-text confirmation, 1 sentence (for simple actions)

Actions:
  allowlist_sender   add senders/domains so future emails skip the unsubscribe action
  remove_allowlist   remove a sender/domain from the allowlist
  list_allowlist     show all allowlisted senders
  help               show available commands
  query_history      show recent triage decisions for a sender or domain
  add_correction     tell Aperture that emails from a sender belong in a different category
  list_corrections   show corrections waiting for approval
  view_prompt        show the current learned prompt rules
  modify_prompt      add or change a rule in the learned prompt
  query_stats        show triage statistics (count by action/category)
  health_status      show queue depths and system health
  unknown            couldn't understand the request

Examples:
  "add nytimes.com and substack.com to my allowlist"
    → {"action":"allowlist_sender","emails":["nytimes.com","substack.com"],"reply":"Added 2 senders to your allowlist."}
  "emails from deals@amazon.com should be category 10"
    → {"action":"add_correction","sender":"deals@amazon.com","wrong_category":0,"correct_category":10,"reply":"Added correction rule for deals@amazon.com."}
  "add a rule: all bank security alerts should be category 1"
    → {"action":"modify_prompt","rule":"all bank security alerts should be category 1","reply":"Updating the learned prompt."}
  "how many emails this week?"
    → {"action":"query_stats","period":"week","reply":"Fetching stats."}
  "what happened to emails from apple.com?"
    → {"action":"query_history","query":"apple.com","reply":"Searching triage history."}
  "help"
    → {"action":"help","reply":"Here's what I can do."}
"""

_HELP_TEXT = """\
<b>Aperture Commands</b>

<b>Allowlist</b>
  • "keep emails from sender@example.com"
  • "add domain.com and other.com to my allowlist"
  • "remove domain.com from allowlist"
  • "show my allowlist"

<b>Corrections</b>
  • "emails from x@example.com should be category 5"
  • "show pending corrections"

<b>Prompt</b>
  • "show learned prompt"
  • "add a rule: treat all receipts as category 10"

<b>History &amp; Stats</b>
  • "what happened to emails from nytimes.com?"
  • "show stats for this week"
  • "stats today"

<b>System</b>
  • "system status" / "health"
  • "help"\
"""


async def handle_message(text: str, db: firestore.Client, telegram) -> None:
    """Parse a freeform Telegram message and execute the inferred command."""
    client = genai.Client(api_key=settings.gemini_api_key)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_INTENT_PROMPT,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        data = json.loads(response.text)
    except Exception as exc:
        logger.error(f"Command parsing failed: {exc}")
        await telegram.send_text("Sorry, I couldn't process that request.")
        return

    action = data.get("action", "unknown")
    reply_text = data.get("reply", "Done.")
    logger.info(f"Command: action={action} | input='{text[:60]}'")

    if action == "allowlist_sender":
        await _cmd_allowlist_sender(data, db, telegram, reply_text)

    elif action == "remove_allowlist":
        await _cmd_remove_allowlist(data, db, telegram)

    elif action == "list_allowlist":
        await _cmd_list_allowlist(db, telegram)

    elif action == "help":
        await telegram.send_text(_HELP_TEXT)

    elif action == "query_history":
        await _cmd_query_history(data, db, telegram)

    elif action == "add_correction":
        await _cmd_add_correction(data, db, telegram, reply_text)

    elif action == "list_corrections":
        await _cmd_list_corrections(db, telegram)

    elif action == "view_prompt":
        await _cmd_view_prompt(db, telegram)

    elif action == "modify_prompt":
        await _cmd_modify_prompt(data, db, telegram, client)

    elif action == "query_stats":
        await _cmd_query_stats(data, db, telegram)

    elif action == "health_status":
        await _cmd_health_status(db, telegram)

    else:
        await telegram.send_text(
            "I didn't understand that. Send <b>help</b> for a list of available commands."
        )


# ── Command handlers ──────────────────────────────────────────────────────────

async def _cmd_allowlist_sender(data: dict, db, telegram, reply_text: str) -> None:
    emails = data.get("emails") or []
    if not emails:
        single = data.get("email", "").strip().lower()
        if single:
            emails = [single]
    if not emails:
        await telegram.send_text("I couldn't find an email address or domain to allowlist.")
        return
    added, skipped = [], []
    for e in emails:
        e = e.strip().lower()
        if e:
            already = _upsert_allowlist(db, e)
            (skipped if already else added).append(e)
    parts = []
    if added:
        parts.append(f"Added: {', '.join(added)}")
    if skipped:
        parts.append(f"Already listed: {', '.join(skipped)}")
    await telegram.send_text("\n".join(parts) or reply_text)


async def _cmd_remove_allowlist(data: dict, db, telegram) -> None:
    email = data.get("email", "").strip().lower()
    if not email:
        await telegram.send_text("I couldn't find an email address or domain to remove.")
        return
    removed = _remove_from_allowlist(db, email)
    if removed:
        await telegram.send_text(f"Removed <b>{email}</b> from your allowlist.")
    else:
        await telegram.send_text(f"<b>{email}</b> wasn't in your allowlist.")


async def _cmd_list_allowlist(db, telegram) -> None:
    docs = list(db.collection("aperture_sender_allowlist").stream())
    if not docs:
        await telegram.send_text("Your sender allowlist is empty.")
        return
    entries = sorted(d.to_dict().get("email", "?") for d in docs)
    lines = ["<b>Allowlisted senders:</b>"] + [f"  • {e}" for e in entries]
    await telegram.send_text("\n".join(lines))


async def _cmd_query_history(data: dict, db, telegram) -> None:
    query = data.get("query", "").strip().lower()
    if not query:
        await telegram.send_text("I couldn't find a sender or domain to search for.")
        return
    all_docs = list(
        db.collection("aperture_triage_log")
        .order_by("processed_at", direction=firestore.Query.DESCENDING)
        .limit(500)
        .stream()
    )
    matches = [
        d.to_dict() for d in all_docs
        if query in d.to_dict().get("sender", "").lower()
        or query in d.to_dict().get("subject", "").lower()
    ][:15]

    if not matches:
        await telegram.send_text(f"No triage history found for <b>{query}</b>.")
        return

    tz = ZoneInfo(settings.timezone)
    lines = [f"<b>Triage history for '{query}'</b> (last {len(matches)}):\n"]
    for item in matches:
        ts = item.get("processed_at")
        time_str = ts.astimezone(tz).strftime("%-m/%-d %-I:%M %p") if ts else "?"
        cat = item.get("category", "?")
        action = item.get("action", "?")
        subject = item.get("subject", "(no subject)")[:55]
        lines.append(f"<b>{time_str}</b> — Cat {cat} / {action}\n  {subject}")
    await telegram.send_text("\n".join(lines))


async def _cmd_add_correction(data: dict, db, telegram, reply_text: str) -> None:
    sender = data.get("sender", "").strip().lower()
    correct_cat = int(data.get("correct_category") or 0)
    wrong_cat = int(data.get("wrong_category") or 0)

    if not sender or not correct_cat:
        await telegram.send_text(
            "I need a sender and a target category. "
            "Try: \"emails from sender@example.com should be category 5\""
        )
        return

    correct_name = CATEGORY_NAMES.get(correct_cat, str(correct_cat))
    wrong_name = CATEGORY_NAMES.get(wrong_cat, str(wrong_cat)) if wrong_cat else "unknown"

    db.collection("aperture_corrections").add({
        "message_id": "",
        "sender": sender,
        "subject": "",
        "snippet": f"Rule added via Telegram command",
        "wrong_category": wrong_cat,
        "wrong_category_name": wrong_name,
        "correct_category": correct_cat,
        "correct_category_name": correct_name,
        "confirmed": True,
        "created_at": firestore.SERVER_TIMESTAMP,
    })
    logger.info(f"Correction added via command: sender={sender} cat={correct_cat}")
    await telegram.send_text(
        f"Correction saved: emails from <b>{sender}</b> → "
        f"Cat {correct_cat} ({correct_name})."
    )


async def _cmd_list_corrections(db, telegram) -> None:
    docs = list(
        db.collection("aperture_corrections")
        .where(filter=FieldFilter("confirmed", "==", False))
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(10)
        .stream()
    )
    if not docs:
        await telegram.send_text("No pending corrections.")
        return

    await telegram.send_text(f"<b>Pending corrections ({len(docs)}):</b>")
    for doc in docs:
        item = doc.to_dict()
        sender = item.get("sender", "?")[:40]
        subject = item.get("subject", "(no subject)")[:50]
        wrong = item.get("wrong_category", "?")
        wrong_name = item.get("wrong_category_name", "")
        correct = item.get("correct_category", "?")
        correct_name = item.get("correct_category_name", "")
        text = (
            f"From: <b>{sender}</b>\n"
            f"Subject: {subject}\n"
            f"Cat {wrong} ({wrong_name}) → Cat {correct} ({correct_name})"
        )
        keyboard = [[{"text": "✓ Approve", "callback_data": f"approve_correction:{doc.id}"}]]
        await telegram.send_with_keyboard(text, keyboard)


async def _cmd_view_prompt(db, telegram) -> None:
    try:
        doc = db.collection("aperture_config").document("prompt_learned").get()
        content = doc.to_dict().get("content", "").strip() if doc.exists else ""
    except Exception as exc:
        await telegram.send_text(f"Error reading prompt: {exc}")
        return

    if not content:
        await telegram.send_text("The learned prompt is currently empty.")
        return

    # Telegram message limit is 4096 chars
    header = "<b>Learned Prompt:</b>\n\n"
    body = content if len(header) + len(content) <= 4000 else content[:4000 - len(header)] + "\n…(truncated)"
    await telegram.send_text(header + body)


async def _cmd_modify_prompt(data: dict, db, telegram, client) -> None:
    rule = data.get("rule", "").strip()
    if not rule:
        await telegram.send_text("I couldn't identify a rule to add. Try: \"add a rule: treat X as category Y\"")
        return

    try:
        doc = db.collection("aperture_config").document("prompt_learned").get()
        current = doc.to_dict().get("content", "").strip() if doc.exists else ""
    except Exception as exc:
        await telegram.send_text(f"Error reading prompt: {exc}")
        return

    edit_prompt = (
        "You are a prompt editor for an email triage system. "
        "Update the learned prompt by applying the requested change. "
        "Preserve existing rules unless they conflict. "
        "Return ONLY the updated prompt text with no explanation or markdown."
    )
    user_msg = f"Current learned prompt:\n{current or '(empty)'}\n\nChange requested: {rule}"

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=edit_prompt,
                temperature=0.2,
            ),
        )
        updated = response.text.strip()
    except Exception as exc:
        await telegram.send_text(f"Error updating prompt: {exc}")
        return

    try:
        db.collection("aperture_config").document("prompt_learned").set({
            "content": updated,
            "updated_at": firestore.SERVER_TIMESTAMP,
        })
        from triage.llm_client import invalidate_prompt_cache
        invalidate_prompt_cache()
    except Exception as exc:
        await telegram.send_text(f"Error saving prompt: {exc}")
        return

    logger.info(f"Learned prompt updated via command: rule='{rule[:60]}'")
    preview = updated[:300] + ("…" if len(updated) > 300 else "")
    await telegram.send_text(f"Learned prompt updated.\n\n<b>Preview:</b>\n{preview}")


async def _cmd_query_stats(data: dict, db, telegram) -> None:
    period = data.get("period", "week")
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)

    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        label = "Today"
    elif period == "month":
        since = now - timedelta(days=30)
        label = "Last 30 days"
    elif period == "all":
        since = None
        label = "All time"
    else:
        since = now - timedelta(days=7)
        label = "Last 7 days"

    query = db.collection("aperture_triage_log")
    if since:
        query = query.where(filter=FieldFilter("processed_at", ">=", since))
    docs = list(query.stream())

    if not docs:
        await telegram.send_text(f"No triage data found for {label.lower()}.")
        return

    action_counts: dict[str, int] = {}
    for doc in docs:
        a = doc.to_dict().get("action", "UNKNOWN")
        action_counts[a] = action_counts.get(a, 0) + 1

    _ACTION_EMOJI = {
        "ALERT": "🚨", "SUMMARY": "📋", "INBOX": "📥",
        "ARCHIVE": "📦", "UNSUBSCRIBE": "🧹", "TRASH": "🗑️",
    }
    lines = [f"<b>📊 Stats — {label}</b>", f"Total: {len(docs)} emails\n"]
    for action in ["ALERT", "SUMMARY", "INBOX", "ARCHIVE", "UNSUBSCRIBE", "TRASH"]:
        count = action_counts.get(action, 0)
        if count:
            emoji = _ACTION_EMOJI.get(action, "•")
            lines.append(f"  {emoji} {action}: {count}")
    if "UNKNOWN" in action_counts:
        lines.append(f"  • UNKNOWN: {action_counts['UNKNOWN']}")
    await telegram.send_text("\n".join(lines))


async def _cmd_health_status(db, telegram) -> None:
    now_utc = datetime.now(timezone.utc)
    try:
        summary_pending = len([
            d for d in db.collection("aperture_summary_queue").stream()
            if not d.to_dict().get("dispatched", False)
        ])
        archive_pending = len([
            d for d in db.collection("aperture_archive_queue").stream()
            if not d.to_dict().get("dispatched", False)
        ])
        active_snoozes = len([
            d for d in db.collection("aperture_snoozes").stream()
            if not d.to_dict().get("sent", False)
            and d.to_dict().get("snooze_until", now_utc) > now_utc
        ])
        tz = ZoneInfo(settings.timezone)
        today_start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        today_count = len(list(
            db.collection("aperture_triage_log")
            .where(filter=FieldFilter("processed_at", ">=", today_start))
            .stream()
        ))
    except Exception as exc:
        await telegram.send_text(f"Error fetching status: {exc}")
        return

    lines = [
        "<b>💚 System Status</b>\n",
        f"  📋 Evening digest queue: {summary_pending} pending",
        f"  📦 Morning digest queue: {archive_pending} pending",
        f"  💤 Active snoozes: {active_snoozes}",
        f"  📬 Processed today: {today_count} emails",
    ]
    await telegram.send_text("\n".join(lines))


# ── Allowlist CRUD (also used by executor) ────────────────────────────────────

def _upsert_allowlist(db: firestore.Client, email: str) -> bool:
    """Add email/domain to allowlist. Returns True if it already existed."""
    existing = list(
        db.collection("aperture_sender_allowlist")
        .where(filter=FieldFilter("email", "==", email))
        .limit(1)
        .stream()
    )
    if existing:
        return True
    db.collection("aperture_sender_allowlist").add({
        "email": email,
        "added_at": firestore.SERVER_TIMESTAMP,
    })
    return False


def _remove_from_allowlist(db: firestore.Client, email: str) -> bool:
    """Remove email/domain from allowlist. Returns True if it existed."""
    docs = list(
        db.collection("aperture_sender_allowlist")
        .where(filter=FieldFilter("email", "==", email))
        .limit(1)
        .stream()
    )
    for doc in docs:
        doc.reference.delete()
        return True
    return False


def is_allowlisted(db: firestore.Client, sender: str) -> bool:
    """
    Return True if the sender's email address or domain is in the allowlist.
    Accepts "Name <email@domain.com>" or bare "email@domain.com".
    """
    import re
    match = re.search(r"<([^>]+)>", sender)
    email = match.group(1).lower().strip() if match else sender.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""

    try:
        if list(
            db.collection("aperture_sender_allowlist")
            .where(filter=FieldFilter("email", "==", email))
            .limit(1)
            .stream()
        ):
            return True
        if domain and list(
            db.collection("aperture_sender_allowlist")
            .where(filter=FieldFilter("email", "==", domain))
            .limit(1)
            .stream()
        ):
            return True
    except Exception as exc:
        logger.warning(f"Allowlist check failed for '{sender}': {exc}")
    return False
