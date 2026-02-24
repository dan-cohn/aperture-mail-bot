"""
Pluggable LLM client for email triage.

Default: Gemini 2.0 Flash via google-generativeai (model configurable via GEMINI_MODEL in .env).
Adding a new provider: subclass BaseTriage and register it in get_triage_client().
"""
import json
import logging
from abc import ABC, abstractmethod

import google.generativeai as genai
from pydantic import ValidationError

from config import settings
from triage.prompt import SYSTEM_PROMPT, build_user_message
from triage.schemas import TriageResult

logger = logging.getLogger(__name__)

# Safe fallback when the LLM returns unparseable output.
_FALLBACK = TriageResult(
    category=9,
    is_urgent=False,
    summary="(Triage unavailable — defaulting to General Reading)",
    reasoning="LLM response could not be parsed.",
    suggested_action="INBOX",
)


class BaseTriage(ABC):
    @abstractmethod
    def triage(self, sender: str, subject: str, snippet: str, date: str = "") -> TriageResult:
        ...


class GeminiTriageClient(BaseTriage):
    def __init__(self):
        genai.configure(api_key=settings.gemini_api_key)
        self._model = genai.GenerativeModel(
            model_name=settings.gemini_model,
            system_instruction=SYSTEM_PROMPT,
            generation_config=genai.GenerationConfig(
                response_mime_type="application/json",
                temperature=0.1,  # Low temp → consistent structured output
            ),
        )

    def triage(self, sender: str, subject: str, snippet: str, date: str = "") -> TriageResult:
        user_msg = build_user_message(sender, subject, snippet, date)
        try:
            response = self._model.generate_content(user_msg)
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


def get_triage_client() -> BaseTriage:
    provider = settings.llm_provider.lower()
    if provider == "gemini":
        return GeminiTriageClient()
    raise NotImplementedError(
        f"LLM provider '{provider}' is not yet implemented. "
        "Available: gemini"
    )
