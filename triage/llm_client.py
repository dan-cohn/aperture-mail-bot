"""
Pluggable LLM client for email triage.

Default: Gemini 2.5 Flash via google-genai (model configurable via GEMINI_MODEL in .env).
Adding a new provider: subclass BaseTriage and register it in get_triage_client().

System prompt assembly (in order):
  1. core prompt    — loaded from Firestore (aperture_config/prompt_core),
                      synced from the git-ignored .prompt file on each deploy
  2. learned prompt — freeform rules in Firestore (aperture_config/prompt_learned),
                      editable without a redeploy via scripts/sync_prompt.py
  3. corrections    — per-email feedback confirmed by the user via Telegram

All three are cached in-process for 5 minutes and reloaded without a redeploy.
"""
import json
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from google import genai
from google.cloud.firestore_v1 import FieldFilter
from google.genai import types
from pydantic import ValidationError

from config import settings
from triage.prompt import build_user_message
from triage.schemas import CATEGORY_NAMES, TriageResult

logger = logging.getLogger(__name__)

_FALLBACK = TriageResult(
    category=9,
    is_urgent=False,
    summary="(Triage unavailable — defaulting to General Reading)",
    reasoning="LLM response could not be parsed.",
    suggested_action="INBOX",
)

_CACHE_TTL = 300.0  # 5 minutes for all caches

# (core, learned, timestamp)
_prompt_cache: tuple[str, str, float] | None = None

# (formatted_corrections, timestamp)
_corrections_cache: tuple[str, float] | None = None


def _load_prompts(db) -> tuple[str, str]:
    """
    Load core and learned prompts from Firestore, cached for 5 minutes.
    Falls back to the local .prompt file if Firestore is unavailable.
    """
    global _prompt_cache

    now = time.monotonic()
    if _prompt_cache and (now - _prompt_cache[2]) < _CACHE_TTL:
        return _prompt_cache[0], _prompt_cache[1]

    def _fallback_core() -> str:
        p = Path(__file__).resolve().parent.parent / ".prompt"
        if p.exists():
            logger.warning("Core prompt not in Firestore — fell back to .prompt file.")
            return p.read_text().strip()
        return ""

    if db is None:
        core = _fallback_core()
        _prompt_cache = (core, "", now)
        return core, ""

    try:
        doc = db.collection("aperture_config").document("prompt_core").get()
        core = doc.to_dict().get("content", "") if doc.exists else ""
        if not core:
            core = _fallback_core()
    except Exception as exc:
        logger.warning(f"Failed to load core prompt: {exc}")
        core = _fallback_core()

    try:
        doc = db.collection("aperture_config").document("prompt_learned").get()
        learned = doc.to_dict().get("content", "") if doc.exists else ""
    except Exception as exc:
        logger.warning(f"Failed to load learned prompt: {exc}")
        learned = ""

    _prompt_cache = (core, learned, now)
    logger.debug(f"Prompts loaded: core={len(core)} chars, learned={len(learned)} chars.")
    return core, learned


def invalidate_prompt_cache() -> None:
    global _prompt_cache
    _prompt_cache = None


def _load_corrections(db) -> str:
    """
    Fetch confirmed corrections from Firestore, format as few-shot examples,
    and cache for 5 minutes. Returns empty string if no corrections or db is None.
    """
    global _corrections_cache

    if db is None:
        return ""

    now = time.monotonic()
    if _corrections_cache and (now - _corrections_cache[1]) < _CACHE_TTL:
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
        core, learned = _load_prompts(self._db)
        corrections = _load_corrections(self._db)
        parts = [p for p in [core, learned, corrections] if p]
        system = "\n\n".join(parts)
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
