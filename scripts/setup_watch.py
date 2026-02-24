#!/usr/bin/env python3
"""
Activate (or renew) Gmail push notifications via Gmail API watch().

    python scripts/setup_watch.py

Gmail watch() expires after exactly 7 days.  Re-run this script weekly,
or automate it with Cloud Scheduler → a Cloud Run internal endpoint.

Prerequisites
-------------
* setup_auth.py has been run and tokens are in Firestore.
* The Pub/Sub topic exists and gmail-api-push@system.gserviceaccount.com
  has the roles/pubsub.publisher IAM role on it (see setup guide Step 5).
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import firestore

from config import settings
from gmail.watch import get_watch_state, setup_watch


def main() -> None:
    db = firestore.Client(project=settings.gcp_project_id, database=settings.firestore_database)

    # Show existing watch state, if any
    existing = get_watch_state(db)
    if existing:
        exp_iso = existing.get("expiration_iso", "unknown")
        print(f"[INFO] Existing watch expires: {exp_iso}")
        print("[INFO] Renewing watch…\n")

    print(f"Project      : {settings.gcp_project_id}")
    print(f"Pub/Sub topic: {settings.pubsub_topic_path}\n")

    response = setup_watch(db)

    expiry_ms = int(response["expiration"])
    expiry_dt = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
    renew_by = expiry_dt.strftime("%Y-%m-%d")

    print("Gmail watch is now active.")
    print(f"  History ID  : {response['historyId']}")
    print(f"  Expires     : {expiry_dt.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"\n  ⚠  Renew before {renew_by} by re-running this script.\n")


if __name__ == "__main__":
    main()
