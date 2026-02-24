"""
Aperture — Personal Gmail Triage Agent
FastAPI entry point for Cloud Run.
"""
import base64
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from google.cloud import firestore

from actions.executor import execute
from config import settings
from gmail.client import build_gmail_service
from gmail.pubsub_handler import process_notification
from notifications.telegram import TelegramNotifier
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
        f"| llm={settings.llm_provider}"
    )
    yield
    logger.info("Aperture shutting down.")


app = FastAPI(
    title="Aperture",
    description="Personal Gmail Triage Agent",
    version="0.2.0",
    lifespan=lifespan,
)


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
async def health_check():
    return {"status": "ok", "project": settings.gcp_project_id}


# ── Gmail Pub/Sub Webhook ─────────────────────────────────────────────────────

@app.post("/webhook/gmail", status_code=status.HTTP_204_NO_CONTENT, tags=["webhook"])
async def gmail_webhook(request: Request):
    """
    Receives Gmail push notifications forwarded by Google Pub/Sub.

    Always returns 204 to prevent Pub/Sub from retrying.
    Processing errors are logged but do not surface as HTTP errors.
    """
    # ── Parse the Pub/Sub envelope ────────────────────────────────────────────
    body = await request.json()
    try:
        encoded = body["message"]["data"]
        payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    except (KeyError, ValueError) as exc:
        # Malformed message — ack it to prevent endless retries
        logger.error(f"Malformed Pub/Sub envelope: {exc} | raw={str(body)[:200]}")
        return

    email_address = payload.get("emailAddress", "unknown")
    history_id = payload.get("historyId", "")
    logger.info(f"Pub/Sub notification: email={email_address}, historyId={history_id}")

    if not history_id:
        logger.warning("Notification missing historyId — skipping.")
        return

    # ── Process notification ──────────────────────────────────────────────────
    try:
        gmail_service = build_gmail_service(db)
        messages = process_notification(history_id, db, gmail_service)
    except Exception as exc:
        logger.exception(f"Failed to fetch messages for historyId={history_id}: {exc}")
        return

    if not messages:
        return

    logger.info(f"Triaging {len(messages)} new message(s)…")

    # ── Triage + execute each message ─────────────────────────────────────────
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
            # Log and continue — don't let one bad message block the rest
            logger.exception(
                f"Error processing message {msg.get('id')} "
                f"('{msg.get('subject', '')[:60]}'): {exc}"
            )
