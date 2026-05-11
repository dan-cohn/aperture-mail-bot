"""
Aperture — Personal Gmail Triage Agent
FastAPI entry point for Cloud Run.
"""
import base64
import json
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from google.cloud import firestore

from actions.executor import execute
from config import settings
from gmail.client import build_gmail_service
from gmail.pubsub_handler import process_notification
from gmail.watch import setup_watch
from notifications.telegram import TelegramNotifier
from notifications.telegram_commands import handle_message
from notifications.telegram_webhook import handle_callback
from scheduler.digest import send_archive_digest, send_digest
from scheduler.llm_backoff import LLMBackoffManager
from scheduler.snooze import process_snoozes
from scheduler.unsubscribe_reminder import send_unsubscribe_reminder
from triage.llm_client import TriageUnavailableError, get_triage_client

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Shared singletons — initialised once at startup, reused across requests
db: firestore.Client | None = None
telegram: TelegramNotifier | None = None
triage_client = None
backoff_manager = LLMBackoffManager()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, telegram, triage_client
    logger.info("Aperture starting up…")
    db = firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )
    telegram = TelegramNotifier()
    triage_client = get_triage_client(db=db)
    backoff_manager.configure(telegram=telegram, process_fn=_process_message)
    logger.info(
        f"Ready | project={settings.gcp_project_id} "
        f"| db={settings.firestore_database} "
        f"| llm={settings.llm_provider} ({settings.gemini_model})"
    )
    yield
    logger.info("Aperture shutting down.")


async def _process_message(msg: dict) -> None:
    """Triage and execute a single message. Raises TriageUnavailableError on LLM 503."""
    gmail_service = build_gmail_service(db)
    triage_result = triage_client.triage(
        sender=msg["sender"],
        subject=msg["subject"],
        snippet=msg["snippet"],
        date=msg["date"],
    )
    await execute(
        triage=triage_result,
        message_id=msg["id"],
        thread_id=msg["thread_id"],
        sender=msg["sender"],
        subject=msg["subject"],
        gmail_service=gmail_service,
        db=db,
        telegram=telegram,
    )


app = FastAPI(
    title="Aperture",
    description="Personal Gmail Triage Agent",
    version="0.4.0",
    lifespan=lifespan,
)


# ── Auth dependency for internal endpoints ────────────────────────────────────

async def verify_internal_secret(x_aperture_secret: str = Header(...)):
    if not settings.internal_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INTERNAL_SECRET is not configured.",
        )
    if x_aperture_secret.strip() != settings.internal_secret.strip():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health_check():
    return {
        "status": "ok",
        "project": settings.gcp_project_id,
        "model": settings.gemini_model,
    }


# ── Gmail Pub/Sub Webhook ─────────────────────────────────────────────────────

@app.post("/webhook/gmail", status_code=status.HTTP_204_NO_CONTENT, tags=["webhook"])
async def gmail_webhook(request: Request):
    """
    Receives Gmail push notifications forwarded by Google Pub/Sub.
    Always returns 204 to prevent Pub/Sub from retrying.
    """
    body = await request.json()
    try:
        encoded = body["message"]["data"]
        payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (KeyError, ValueError) as exc:
        logger.error(f"Malformed Pub/Sub envelope: {exc} | raw={str(body)[:200]}")
        return

    email_address = payload.get("emailAddress", "unknown")
    history_id    = str(payload.get("historyId", ""))
    logger.info(f"Pub/Sub notification: email={email_address}, historyId={history_id}")

    if not history_id:
        logger.warning("Notification missing historyId — skipping.")
        return

    try:
        messages = process_notification(history_id, db, build_gmail_service(db))
    except Exception as exc:
        logger.exception(f"Failed to fetch messages for historyId={history_id}: {exc}")
        return

    if not messages:
        return

    logger.info(f"Triaging {len(messages)} new message(s)…")

    for msg in messages:
        if backoff_manager.is_blocked():
            backoff_manager.enqueue(msg)
            continue
        try:
            await _process_message(msg)
        except TriageUnavailableError as exc:
            backoff_manager.enqueue(msg)
            await backoff_manager.on_failure(str(exc))
        except Exception as exc:
            logger.exception(
                f"Error processing message {msg.get('id')} "
                f"('{msg.get('subject', '')[:60]}'): {exc}"
            )


# ── Telegram Callback Webhook ─────────────────────────────────────────────────

@app.post("/webhook/telegram", status_code=status.HTTP_200_OK, tags=["webhook"])
async def telegram_webhook(request: Request):
    """
    Receives Telegram callback queries (button taps on alert messages).
    Verified via X-Telegram-Bot-Api-Secret-Token header.
    """
    if settings.telegram_webhook_secret:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    update = await request.json()
    callback_query = update.get("callback_query")
    message = update.get("message")

    if callback_query:
        chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        if chat_id == settings.telegram_chat_id:
            await handle_callback(callback_query, db)

    elif message:
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "").strip()
        if chat_id == settings.telegram_chat_id and text:
            await handle_message(text, db, telegram)

    return {"ok": True}


# ── Internal endpoints (Cloud Scheduler) ─────────────────────────────────────

@app.post(
    "/internal/digest/morning",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_morning_digest():
    """Send the morning archive digest. Triggered at 07:30."""
    count = await send_archive_digest(db, telegram)
    return {"dispatched": count}


@app.post(
    "/internal/digest/evening",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_evening_digest():
    """Send the evening inbox digest. Triggered at 17:30."""
    count = await send_digest(db, telegram)
    return {"dispatched": count}


@app.post(
    "/internal/digest",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
    include_in_schema=False,
)
async def trigger_digest():
    """Legacy alias → evening inbox digest."""
    count = await send_digest(db, telegram)
    return {"dispatched": count}


@app.post(
    "/internal/unsubscribe-reminder",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_unsubscribe_reminder():
    """Send the weekly Aperture/Unsubscribe summary. Triggered Sundays 10:00."""
    gmail_service = build_gmail_service(db)
    count = await send_unsubscribe_reminder(db, gmail_service, telegram)
    return {"found": count}


@app.post(
    "/internal/renew-watch",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_renew_watch():
    """Renew the Gmail push watch (expires every 7 days). Triggered every 5 days."""
    response = setup_watch(db)
    return {"history_id": response["historyId"], "expiration": response["expiration"]}


@app.post(
    "/internal/process-snoozes",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_process_snoozes():
    """Re-fire alerts for expired snoozes. Triggered every 15 minutes."""
    count = await process_snoozes(db, telegram)
    return {"fired": count}


@app.post(
    "/internal/dashboard/stop",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_dashboard_stop():
    """Scale aperture-dashboard to min-instances=0. Called by Cloud Tasks after 1h."""
    from notifications.telegram_commands import _revert_dashboard
    await _revert_dashboard(db, telegram)
    return {"ok": True}
