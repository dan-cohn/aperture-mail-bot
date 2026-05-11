"""
LLM backoff manager.

Handles transient Gemini API failures with an escalating retry strategy:

  1st failure  → wait 5 min, retry
  2nd failure  → wait 10 min, retry
  3rd failure  → send Telegram alert, wait 5 min, retry (counter resets)

While in backoff, incoming messages are queued and drained after recovery.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

# Backoff delays (seconds) indexed by failure count (1-based)
_DELAYS = {1: 300, 2: 600}
_POST_ALERT_DELAY = 300


class LLMBackoffManager:
    def __init__(self):
        self._failure_count = 0
        self._blocked = False
        self._pending: list[dict] = []
        self._retry_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._telegram = None
        self._process_fn = None  # async fn(msg: dict) -> None, raises TriageUnavailableError

    def configure(self, telegram, process_fn) -> None:
        self._telegram = telegram
        self._process_fn = process_fn

    def is_blocked(self) -> bool:
        return self._blocked

    def enqueue(self, msg: dict) -> None:
        self._pending.append(msg)
        logger.info(f"Backoff queue: +1 message (depth={len(self._pending)})")

    async def on_failure(self, exc_str: str) -> None:
        """Called when triage raises TriageUnavailableError. Idempotent under the lock."""
        async with self._lock:
            if self._blocked:
                return  # already handling a backoff cycle

            self._failure_count += 1
            self._blocked = True
            fc = self._failure_count

            if fc >= 3:
                await self._telegram.send_text(
                    f"⚠️ <b>Aperture: LLM Unavailable</b>\n\n"
                    f"Gemini returned 503 three times in a row.\n"
                    f"Error: <code>{exc_str[:300]}</code>\n\n"
                    f"Retrying in 5 minutes."
                )
                delay = _POST_ALERT_DELAY
                self._failure_count = 0  # reset counter for next cycle
            else:
                delay = _DELAYS[fc]

            logger.warning(
                f"LLM backoff: failure #{fc} — "
                f"sleeping {delay}s before retry | queue={len(self._pending)}"
            )
            self._retry_task = asyncio.create_task(self._retry_after(delay))

    async def _retry_after(self, delay: int) -> None:
        """Sleep, then attempt to process the first queued message."""
        await asyncio.sleep(delay)

        if not self._pending:
            async with self._lock:
                self._blocked = False
                self._failure_count = 0
            return

        msg = self._pending[0]
        logger.info(
            f"LLM backoff: retrying '{msg.get('subject', '')[:50]}' "
            f"(queue depth={len(self._pending)})"
        )

        from triage.llm_client import TriageUnavailableError
        try:
            await self._process_fn(msg)
        except TriageUnavailableError as exc:
            logger.warning(f"LLM backoff: retry failed again: {exc}")
            async with self._lock:
                self._blocked = False  # allow on_failure to re-engage
            await self.on_failure(str(exc))
            return
        except Exception as exc:
            # Non-retriable error — log, discard, and continue
            logger.exception(f"LLM backoff: non-retriable error on retry: {exc}")

        self._pending.pop(0)
        async with self._lock:
            self._blocked = False
            self._failure_count = 0

        logger.info(
            f"LLM backoff: retry succeeded — draining {len(self._pending)} queued message(s)"
        )
        await self._drain_queue()

    async def _drain_queue(self) -> None:
        """Process all remaining queued messages after recovery."""
        from triage.llm_client import TriageUnavailableError
        while self._pending:
            msg = self._pending[0]
            try:
                await self._process_fn(msg)
                self._pending.pop(0)
            except TriageUnavailableError as exc:
                logger.warning(f"LLM backoff: drain hit 503 — re-entering backoff")
                await self.on_failure(str(exc))
                return
            except Exception as exc:
                logger.exception(
                    f"LLM backoff: drain error for '{msg.get('subject', '')[:50]}': {exc}"
                )
                self._pending.pop(0)
