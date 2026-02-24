"""
Pluggable LLM client for email triage.

Default: Gemini 2.5 Flash via google-genai (model configurable via GEMINI_MODEL in .env).
Adding a new provider: subclass BaseTriage and register it in get_triage_client().

Dynamic corrections:
  When a Firestore client is passed to GeminiTriageClient, confirmed user corrections
  are fetched (cached for 5 min) and appended to the system prompt as high-priority
  few-shot examples. This means feedback takes effect immediately — no redeploy needed.
"""
import json
import logging
import time
from abc import ABC, abstractmethod

from google import genai
from google.cloud.firestore_v1 import FieldFilter
from google.genai import types
from pydantic import ValidationError

from config import settings
from triage.prompt import SYSTEM_PROMPT, build_user_message
from triage.schemas import CATEGORY_NAMES, TriageResult

logger = logging.getLogger(__name__)

# Safe fallback when the LLM returns unparseable output.
_FALLBACK = TriageResult(
    category=9,
    is_urgent=False,
    summary="(Triage unavailable — defaulting to General Reading)",
    reasoning="LLM response could not be parsed.",
    suggested_action="INBOX",
)

# In-process corrections cache: (formatted_string, timestamp)
_corrections_cache: tuple[str, float] | None = None
_CORRECTIONS_TTL = 300.0  # 5 minutes


def _load_corrections(db) -> str:
    """
    Fetch confirmed corrections from Firestore, format as few-shot examples,
    and cache the result for 5 minutes.
    Returns an empty string if there are no corrections or db is None.
    """
    global _corrections_cache

    if db is None:
        return ""

    now = time.monotonic()
    if _corrections_cache and (now - _corrections_cache[1]) < _CORRECTIONS_TTL:
        return _corrections_cache[0]

    try:
        docs = (
            db.collection("aperture_corrections")
            .where(filter=FieldFilter("confirmed", "==", True))
            .limit(20)
            .stream()
        )
        corrections = [doc.to_dict() for doc in docs]
    except Exception as exc:
        logger.warning(f"Failed to load corrections from Firestore: {exc}")
        _corrections_cache = ("", now)
        return ""

    if not corrections:
        _corrections_cache = ("", now)
        return ""

    lines = [
        "\n**User-Confirmed Corrections** "
        "(these override all other rules — follow them exactly):\n"
    ]
    for c in corrections:
        lines.append(
            f"USER CORRECTION — Cat {c['correct_category']} NOT Cat {c['wrong_category']}\n"
            f"From: {c.get('sender', '')}\n"
            f"Subject: {c.get('subject', '')}\n"
            f"Snippet: {c.get('snippet', '')}\n"
            f"→ Category: {c['correct_category']} "
            f"({CATEGORY_NAMES.get(c['correct_category'], '')})\n"
            f"→ Was wrongly classified as: {c['wrong_category']} "
            f"({CATEGORY_NAMES.get(c['wrong_category'], '')})"
        )

    formatted = "\n\n".join(lines)
    _corrections_cache = (formatted, now)
    logger.debug(f"Loaded {len(corrections)} confirmed correction(s) into prompt.")
    return formatted


def invalidate_corrections_cache() -> None:
    """Call this after a correction is confirmed to force an immediate reload."""
    global _corrections_cache
    _corrections_cache = None


class BaseTriage(ABC):
    @abstractmethod
    def triage(self, sender: str, subject: str, snippet: str, date: str = "") -> TriageResult:
        ...


class GeminiTriageClient(BaseTriage):
    def __init__(self, db=None):
        self._db = db
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _get_config(self) -> types.GenerateContentConfig:
        """
        Build a GenerateContentConfig whose system instruction includes any confirmed
        corrections fetched from Firestore. Rebuilt each call so corrections
        take effect within the cache TTL (5 min) without a redeploy.
        """
        corrections = _load_corrections(self._db)
        system = SYSTEM_PROMPT + corrections if corrections else SYSTEM_PROMPT
        return types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            temperature=0.1,
        )

    def triage(self, sender: str, subject: str, snippet: str, date: str = "") -> TriageResult:
        user_msg = build_user_message(sender, subject, snippet, date)
        response = None
        try:
            response = self._client.models.generate_content(
                model=settings.gemini_model,
                contents=user_msg,
                config=self._get_config(),
            )
            data = json.loads(response.text)
            result = TriageResult(**data)
            logger.info(
                f"Triage: cat={result.category} ({result.category_name}) | "
                f"action={result.action} | subject='{subject[:60]}'"
            )
            return result
        except (json.JSONDecodeError, ValidationError, Exception) as exc:
            logger.error(
                f"Triage failed for '{subject[:60]}': {exc}\n"
                f"Raw LLM output: {getattr(response, 'text', 'N/A')}"
            )
            return _FALLBACK


def get_triage_client(db=None) -> BaseTriage:
    provider = settings.llm_provider.lower()
    if provider == "gemini":
        return GeminiTriageClient(db=db)
    raise NotImplementedError(
        f"LLM provider '{provider}' is not yet implemented. "
        "Available: gemini"
    )
