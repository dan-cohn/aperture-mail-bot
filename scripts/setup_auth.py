#!/usr/bin/env python3
"""
One-time local script: authorise Aperture with your Gmail account.

    python scripts/setup_auth.py

This opens a browser window for Google OAuth2 consent.  On success it
stores the token in Firestore so Cloud Run never needs local credentials.

Requirements
------------
* credentials.json must exist in the project root (downloaded from GCP Console).
* GOOGLE_APPLICATION_CREDENTIALS or `gcloud auth application-default login`
  must be set so the script can write to Firestore.
* GCP_PROJECT_ID must be set in .env (or the environment).
"""
import sys
from pathlib import Path

# Allow imports from project root regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import firestore
from google_auth_oauthlib.flow import InstalledAppFlow

from auth.token_store import save_credentials
from config import settings

SCOPES = [
    # Allows read + label + archive + trash — the minimum Aperture needs.
    # gmail.modify is a superset of gmail.readonly; keep both for clarity.
    "https://www.googleapis.com/auth/gmail.modify",
]

CREDENTIALS_FILE = Path(__file__).resolve().parent.parent / "credentials.json"


def main() -> None:
    if not CREDENTIALS_FILE.exists():
        print(
            f"\n[ERROR] {CREDENTIALS_FILE} not found.\n"
            "Download it from:\n"
            "  GCP Console → APIs & Services → Credentials\n"
            "  → OAuth 2.0 Client IDs → your client → ⬇ Download JSON\n"
            "Rename the file to credentials.json and place it in the project root.\n"
        )
        sys.exit(1)

    print(f"Project  : {settings.gcp_project_id}")
    print(f"Scopes   : {SCOPES}")
    print("\nStarting OAuth2 flow — a browser window will open…\n")

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
    creds = flow.run_local_server(port=0)

    print("Authorization successful.  Saving credentials to Firestore…")
    db = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_database)
    save_credentials(creds, db)

    print(
        f"\nDone!  Credentials stored in Firestore "
        f"(project={settings.gcp_project_id}, "
        f"collection=aperture_config, doc=oauth_tokens).\n"
        "You can now run:  python scripts/setup_watch.py\n"
    )


if __name__ == "__main__":
    main()
