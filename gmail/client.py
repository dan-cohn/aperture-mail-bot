"""
Thin wrapper around the Gmail API v1 service object.
Centralises auth and service construction; avoids re-building per request.
"""
from google.cloud import firestore
from googleapiclient.discovery import build

from auth.gmail_auth import get_valid_credentials

_USER = "me"
_METADATA_HEADERS = ["From", "Subject", "Date"]


def build_gmail_service(db: firestore.Client):
    """Return an authorised Gmail API service object."""
    creds = get_valid_credentials(db)
    # cache_discovery=False avoids a file-system write on Cloud Run
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_message(service, message_id: str, fmt: str = "full") -> dict:
    """Fetch a single Gmail message by ID."""
    return (
        service.users()
        .messages()
        .get(userId=_USER, id=message_id, format=fmt)
        .execute()
    )


def get_message_metadata(service, message_id: str) -> dict:
    """
    Fetch From/Subject/Date headers + snippet for a message.
    Much lighter than format=full — avoids downloading the entire body.
    Returns a flat dict ready for triage.
    """
    msg = (
        service.users()
        .messages()
        .get(
            userId=_USER,
            id=message_id,
            format="metadata",
            metadataHeaders=_METADATA_HEADERS,
        )
        .execute()
    )
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "sender": headers.get("From", "Unknown"),
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "label_ids": msg.get("labelIds", []),
    }


def get_history(service, start_history_id: str, history_types: list[str] | None = None) -> dict:
    """
    Fetch all Gmail history records since *start_history_id*, handling pagination.
    historyTypes filters to 'messageAdded', 'messageDeleted', etc.
    """
    kwargs: dict = {"userId": _USER, "startHistoryId": start_history_id}
    if history_types:
        kwargs["historyTypes"] = history_types

    all_history: list[dict] = []
    latest_history_id: str = start_history_id

    while True:
        response = service.users().history().list(**kwargs).execute()
        all_history.extend(response.get("history", []))
        latest_history_id = response.get("historyId", latest_history_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            break
        kwargs["pageToken"] = page_token

    return {"history": all_history, "historyId": latest_history_id}


def modify_message(service, message_id: str, add_labels: list[str], remove_labels: list[str]) -> dict:
    """Add/remove label IDs on a message."""
    return (
        service.users()
        .messages()
        .modify(
            userId=_USER,
            id=message_id,
            body={"addLabelIds": add_labels, "removeLabelIds": remove_labels},
        )
        .execute()
    )


def trash_message(service, message_id: str) -> dict:
    """Move a message to Trash."""
    return service.users().messages().trash(userId=_USER, id=message_id).execute()


def get_or_create_label(service, name: str) -> str:
    """Return the label ID for *name*, creating it if it doesn't exist."""
    existing = service.users().labels().list(userId=_USER).execute().get("labels", [])
    for label in existing:
        if label["name"] == name:
            return label["id"]

    created = (
        service.users()
        .labels()
        .create(
            userId=_USER,
            body={
                "name": name,
                "messageListVisibility": "hide",
                "labelListVisibility": "labelHide",
            },
        )
        .execute()
    )
    return created["id"]
