"""
Natural language command handler for inbound Telegram messages.

Parses freeform text via Gemini and executes the inferred action against
Firestore. Supported commands (extensible):

  allowlist_sender  — keep a sender/domain from being auto-unsubscribed
  remove_allowlist  — remove a sender/domain from the allowlist
  list_allowlist    — show all allowlisted senders
"""
import json
import logging

from google import genai
from google.cloud import firestore
from google.cloud.firestore_v1 import FieldFilter
from google.genai import types

from config import settings

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a command parser for Aperture, a personal email triage assistant.
Parse the user's natural language message and return JSON with:
  - action: one of "allowlist_sender", "remove_allowlist", "list_allowlist", "unknown"
  - email: sender email address or domain to act on (omit for list/unknown)
  - reply: brief plain-text confirmation to send back to the user (1 sentence)

"Allowlist" means: future emails from this sender/domain will stay in the
inbox instead of being flagged for unsubscribing.

Examples:
  "keep emails from newsletter@example.com"
    → {"action": "allowlist_sender", "email": "newsletter@example.com",
       "reply": "Added newsletter@example.com to your sender allowlist."}

  "don't unsubscribe from substack.com"
    → {"action": "allowlist_sender", "email": "substack.com",
       "reply": "Added substack.com to your sender allowlist."}

  "remove example.com from my allowlist"
    → {"action": "remove_allowlist", "email": "example.com",
       "reply": "Removed example.com from your sender allowlist."}

  "show my allowlist"
    → {"action": "list_allowlist",
       "reply": "Here's your sender allowlist."}

  "what's the weather?"
    → {"action": "unknown",
       "reply": "I can manage your sender allowlist — try: 'keep emails from sender@example.com'."}
"""


async def handle_message(text: str, db: firestore.Client, reply_fn) -> None:
    """Parse a freeform Telegram message and execute the inferred command."""
    client = genai.Client(api_key=settings.gemini_api_key)

    try:
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=text,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        data = json.loads(response.text)
    except Exception as exc:
        logger.error(f"Command parsing failed: {exc}")
        await reply_fn("Sorry, I couldn't process that request.")
        return

    action = data.get("action", "unknown")
    email = data.get("email", "").strip().lower()
    reply_text = data.get("reply", "Done.")

    if action == "allowlist_sender" and email:
        already = _upsert_allowlist(db, email)
        if already:
            reply_text = f"{email} is already in your sender allowlist."
        logger.info(f"Allowlist add: {email} (already={already})")

    elif action == "remove_allowlist" and email:
        removed = _remove_from_allowlist(db, email)
        if not removed:
            reply_text = f"{email} wasn't in your sender allowlist."
        logger.info(f"Allowlist remove: {email} (found={removed})")

    elif action == "list_allowlist":
        docs = list(db.collection("aperture_sender_allowlist").stream())
        if docs:
            entries = sorted(d.to_dict().get("email", "?") for d in docs)
            reply_text = "Allowlisted senders:\n" + "\n".join(f"• {e}" for e in entries)
        else:
            reply_text = "Your sender allowlist is empty."

    await reply_fn(reply_text)


# ── Allowlist CRUD ────────────────────────────────────────────────────────────

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
