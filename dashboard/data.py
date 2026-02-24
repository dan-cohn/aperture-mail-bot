"""
Firestore + Pub/Sub data fetching for the Aperture dashboard.

All query functions use st.cache_data so the UI stays responsive.
The Firestore client itself uses st.cache_resource (one connection, shared).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


@st.cache_resource
def get_db():
    from google.cloud import firestore
    return firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )


@st.cache_data(ttl=60)
def get_triage_log(_db, limit: int = 300) -> list[dict]:
    """Most recent triage decisions, newest first."""
    from google.cloud import firestore
    docs = (
        _db.collection("aperture_triage_log")
        .order_by("processed_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    rows = []
    for doc in docs:
        d = doc.to_dict()
        # Convert Firestore timestamp → Python datetime
        if d.get("processed_at"):
            d["processed_at"] = d["processed_at"].replace(tzinfo=timezone.utc)
        rows.append(d)
    return rows


@st.cache_data(ttl=30)
def get_summary_queue(_db) -> list[dict]:
    """Undispatched items waiting for the next digest."""
    from google.cloud import firestore
    docs = _db.collection("aperture_summary_queue").stream()
    rows = []
    for doc in docs:
        d = doc.to_dict()
        if d.get("dispatched", False):
            continue
        if d.get("enqueued_at"):
            d["enqueued_at"] = d["enqueued_at"].replace(tzinfo=timezone.utc)
        rows.append(d)
    return rows


@st.cache_data(ttl=60)
def get_watch_state(_db) -> dict:
    doc = _db.collection("aperture_config").document("gmail_watch").get()
    return doc.to_dict() if doc.exists else {}


@st.cache_data(ttl=30)
def get_control_state(_db) -> dict:
    doc = _db.collection("aperture_config").document("control_state").get()
    return doc.to_dict() if doc.exists else {}


@st.cache_data(ttl=30)
def get_subscription_state() -> str:
    """Return 'RUNNING', 'PAUSED', or 'UNKNOWN'."""
    try:
        from google.cloud import pubsub_v1
        client = pubsub_v1.SubscriberClient()
        sub = client.get_subscription(
            request={"subscription": settings.pubsub_subscription_path}
        )
        return "RUNNING" if sub.push_config.push_endpoint else "PAUSED"
    except Exception:
        return "UNKNOWN"


def pause_subscription() -> None:
    from google.cloud import pubsub_v1
    from google.cloud.pubsub_v1.types import PushConfig
    client = pubsub_v1.SubscriberClient()
    client.modify_push_config(
        request={
            "subscription": settings.pubsub_subscription_path,
            "push_config": PushConfig(),
        }
    )
    db = get_db()
    db.collection("aperture_config").document("control_state").set({
        "state": "paused",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    get_subscription_state.clear()
    get_control_state.clear()


@st.cache_data(ttl=30)
def get_corrections(_db) -> list[dict]:
    """All corrections (confirmed and pending), newest first."""
    docs = (
        _db.collection("aperture_corrections")
        .order_by("created_at", direction="DESCENDING")
        .limit(100)
        .stream()
    )
    rows = []
    for doc in docs:
        d = doc.to_dict()
        d["_doc_id"] = doc.id
        if d.get("created_at"):
            d["created_at"] = d["created_at"].replace(tzinfo=timezone.utc)
        rows.append(d)
    return rows


def confirm_correction(doc_id: str, db) -> None:
    """Mark a correction as confirmed and invalidate the LLM cache."""
    from triage.llm_client import invalidate_corrections_cache
    db.collection("aperture_corrections").document(doc_id).update({
        "confirmed": True,
        "confirmed_at": datetime.now(timezone.utc),
    })
    invalidate_corrections_cache()
    get_corrections.clear()


def discard_correction(doc_id: str, db) -> None:
    """Delete a correction."""
    db.collection("aperture_corrections").document(doc_id).delete()
    get_corrections.clear()


def resume_subscription() -> None:
    from google.cloud import pubsub_v1
    from google.cloud.pubsub_v1.types import PushConfig
    if not settings.cloud_run_url:
        raise ValueError("CLOUD_RUN_URL is not set in .env")
    push_endpoint = f"{settings.cloud_run_url.rstrip('/')}/webhook/gmail"
    client = pubsub_v1.SubscriberClient()
    client.modify_push_config(
        request={
            "subscription": settings.pubsub_subscription_path,
            "push_config": PushConfig(push_endpoint=push_endpoint),
        }
    )
    db = get_db()
    db.collection("aperture_config").document("control_state").set({
        "state": "running",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    get_subscription_state.clear()
    get_control_state.clear()
