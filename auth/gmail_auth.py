"""
Provides a valid, auto-refreshing Gmail OAuth2 credential object.

Usage (anywhere in the app):
    from auth.gmail_auth import get_valid_credentials
    creds = get_valid_credentials(db)
    service = build("gmail", "v1", credentials=creds)
"""
import logging

from google.auth.transport.requests import Request
from google.cloud import firestore

from auth.token_store import load_credentials, save_credentials

logger = logging.getLogger(__name__)


def get_valid_credentials(db: firestore.Client):
    """
    Load credentials from Firestore, refreshing the access token if expired.
    Raises RuntimeError if no credentials have been stored yet.
    """
    creds = load_credentials(db)

    if creds is None:
        raise RuntimeError(
            "No OAuth2 credentials in Firestore. "
            "Run `python scripts/setup_auth.py` first."
        )

    if creds.expired and creds.refresh_token:
        logger.info("Access token expired — refreshing…")
        creds.refresh(Request())
        save_credentials(creds, db)
        logger.info("Access token refreshed and saved to Firestore.")

    return creds
