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
from scheduler.digest import send_digest
from scheduler.unsubscribe_reminder import send_unsubscribe_reminder
from triage.llm_client import get_triage_client

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

# Shared singletons — initialised once at startup, reused across requests
db: firestore.Client | None = None
telegram: TelegramNotifier | None = None
triage_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, telegram, triage_client
    logger.info("Aperture starting up…")
    db = firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )
    telegram = TelegramNotifier()
    triage_client = get_triage_client()
    logger.info(
        f"Ready | project={settings.gcp_project_id} "
        f"| db={settings.firestore_database} "
        f"| llm={settings.llm_provider} ({settings.gemini_model})"
    )
    yield
    logger.info("Aperture shutting down.")


app = FastAPI(
    title="Aperture",
    description="Personal Gmail Triage Agent",
    version="0.3.0",
    lifespan=lifespan,
)


# ── Auth dependency for internal endpoints ────────────────────────────────────

async def verify_internal_secret(x_aperture_secret: str = Header(...)):
    """
    Protects /internal/* endpoints.
    Cloud Scheduler sends the secret via the X-Aperture-Secret header.
    """
    if not settings.internal_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="INTERNAL_SECRET is not configured.",
        )
    if x_aperture_secret != settings.internal_secret:
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
    Processing errors are logged but do not surface as HTTP errors.
    """
    body = await request.json()
    try:
        encoded = body["message"]["data"]
        payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (KeyError, ValueError) as exc:
        logger.error(f"Malformed Pub/Sub envelope: {exc} | raw={str(body)[:200]}")
        return

    email_address = payload.get("emailAddress", "unknown")
    history_id = payload.get("historyId", "")
    logger.info(f"Pub/Sub notification: email={email_address}, historyId={history_id}")

    if not history_id:
        logger.warning("Notification missing historyId — skipping.")
        return

    try:
        gmail_service = build_gmail_service(db)
        messages = process_notification(history_id, db, gmail_service)
    except Exception as exc:
        logger.exception(f"Failed to fetch messages for historyId={history_id}: {exc}")
        return

    if not messages:
        return

    logger.info(f"Triaging {len(messages)} new message(s)…")

    for msg in messages:
        try:
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
        except Exception as exc:
            logger.exception(
                f"Error processing message {msg.get('id')} "
                f"('{msg.get('subject', '')[:60]}'): {exc}"
            )


# ── Internal endpoints (Cloud Scheduler) ─────────────────────────────────────

@app.post(
    "/internal/digest",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_digest():
    """
    Send the daily email digest to Telegram.
    Triggered by Cloud Scheduler at 07:30 and 17:30.
    """
    count = await send_digest(db, telegram)
    return {"dispatched": count}


@app.post(
    "/internal/unsubscribe-reminder",
    status_code=status.HTTP_200_OK,
    tags=["internal"],
    dependencies=[Depends(verify_internal_secret)],
)
async def trigger_unsubscribe_reminder():
    """
    Send the weekly Aperture/Unsubscribe summary to Telegram.
    Triggered by Cloud Scheduler every Sunday at 10:00.
    """
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
    """
    Renew the Gmail push notification watch (expires every 7 days).
    Triggered by Cloud Scheduler every 5 days.
    """
    response = setup_watch(db)
    return {
        "history_id": response["historyId"],
        "expiration": response["expiration"],
    }
