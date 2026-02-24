"""
Firestore-backed OAuth2 token storage.

Schema:
  Collection : aperture_config
  Document   : oauth_tokens
  Fields     : token, refresh_token, token_uri, client_id,
               client_secret, scopes, expiry, updated_at
"""
import logging
from datetime import datetime, timezone

from google.cloud import firestore
from google.oauth2.credentials import Credentials

logger = logging.getLogger(__name__)

_COLLECTION = "aperture_config"
_DOC = "oauth_tokens"


def save_credentials(creds: Credentials, db: firestore.Client) -> None:
    """Persist OAuth2 credentials to Firestore (full overwrite)."""
    doc_ref = db.collection(_COLLECTION).document(_DOC)
    doc_ref.set(
        {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes) if creds.scopes else [],
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    logger.info("OAuth2 credentials saved to Firestore.")


def load_credentials(db: firestore.Client) -> Credentials | None:
    """Load OAuth2 credentials from Firestore. Returns None if not found."""
    doc_ref = db.collection(_COLLECTION).document(_DOC)
    doc = doc_ref.get()

    if not doc.exists:
        logger.warning("No OAuth2 credentials found in Firestore.")
        return None

    data = doc.to_dict()
    expiry = datetime.fromisoformat(data["expiry"]) if data.get("expiry") else None

    return Credentials(
        token=data["token"],
        refresh_token=data["refresh_token"],
        token_uri=data["token_uri"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=data.get("scopes"),
        expiry=expiry,
    )
