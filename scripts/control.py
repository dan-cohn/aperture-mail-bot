#!/usr/bin/env python3
"""
Aperture control — pause, resume, or check status.

    python scripts/control.py pause    # Stop processing; messages queue in Pub/Sub
    python scripts/control.py resume   # Resume; queued messages are delivered
    python scripts/control.py status   # Show current state + queue depth

How it works:
  pause  — converts the Pub/Sub push subscription to pull mode.
            Cloud Run stops receiving webhooks; messages accumulate
            in the subscription for up to 7 days.
  resume — converts it back to push, pointing at your Cloud Run URL.
            Pub/Sub immediately starts delivering the backlog.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import pubsub_v1
from google.cloud.pubsub_v1.types import PushConfig

from config import settings

# ── Firestore state tracking ──────────────────────────────────────────────────
_COLLECTION = "aperture_config"
_CONTROL_DOC = "control_state"


def _get_db():
    from google.cloud import firestore
    return firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )


def _save_state(state: str) -> None:
    db = _get_db()
    db.collection(_COLLECTION).document(_CONTROL_DOC).set({
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def _load_state() -> str | None:
    db = _get_db()
    doc = db.collection(_COLLECTION).document(_CONTROL_DOC).get()
    return doc.to_dict().get("state") if doc.exists else None


# ── Pub/Sub helpers ───────────────────────────────────────────────────────────

def _get_subscription():
    client = pubsub_v1.SubscriberClient()
    return client, client.get_subscription(
        request={"subscription": settings.pubsub_subscription_path}
    )


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_pause() -> None:
    client, sub = _get_subscription()

    if not sub.push_config.push_endpoint:
        print("Already PAUSED — subscription is already in pull mode.")
        return

    client.modify_push_config(
        request={
            "subscription": settings.pubsub_subscription_path,
            "push_config": PushConfig(),  # empty = pull mode
        }
    )
    _save_state("paused")

    print("PAUSED.")
    print(f"  Subscription : {settings.pubsub_subscription_path}")
    print( "  Mode         : pull (no delivery to Cloud Run)")
    print( "  Retention    : up to 7 days")
    print()
    print("Run `python scripts/control.py status` to check the queue depth.")
    print("Run `python scripts/control.py resume` when ready to process.")


def cmd_resume() -> None:
    if not settings.cloud_run_url:
        print("ERROR: CLOUD_RUN_URL is not set in .env.")
        print("  Set it to your Cloud Run service URL and try again.")
        sys.exit(1)

    push_endpoint = f"{settings.cloud_run_url.rstrip('/')}/webhook/gmail"

    client, sub = _get_subscription()

    if sub.push_config.push_endpoint == push_endpoint:
        print("Already RUNNING — subscription is already pushing to Cloud Run.")
        return

    client.modify_push_config(
        request={
            "subscription": settings.pubsub_subscription_path,
            "push_config": PushConfig(push_endpoint=push_endpoint),
        }
    )
    _save_state("running")

    print("RESUMED.")
    print(f"  Subscription : {settings.pubsub_subscription_path}")
    print(f"  Push endpoint: {push_endpoint}")
    print()
    print("Pub/Sub will now deliver any queued messages to Cloud Run.")


def cmd_status() -> None:
    client, sub = _get_subscription()
    firestore_state = _load_state() or "unknown"

    endpoint = sub.push_config.push_endpoint
    if endpoint:
        mode = "RUNNING"
        mode_detail = f"push → {endpoint}"
    else:
        mode = "PAUSED"
        mode_detail = "pull (messages accumulating)"

    print(f"Aperture Status")
    print(f"  Mode         : {mode}")
    print(f"  Subscription : {mode_detail}")
    print(f"  Last change  : {firestore_state}")
    print(f"  Project      : {settings.gcp_project_id}")

    # Show approximate backlog from subscription metrics
    try:
        from google.cloud import monitoring_v3
        from google.protobuf import timestamp_pb2
        import time

        m_client = monitoring_v3.MetricServiceClient()
        project_name = f"projects/{settings.gcp_project_id}"
        now = time.time()
        interval = monitoring_v3.TimeInterval(
            end_time={"seconds": int(now)},
            start_time={"seconds": int(now) - 300},
        )
        results = m_client.list_time_series(
            request={
                "name": project_name,
                "filter": (
                    'metric.type="pubsub.googleapis.com/subscription/num_undelivered_messages"'
                    f' AND resource.labels.subscription_id="{settings.pubsub_subscription}"'
                ),
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            }
        )
        for series in results:
            if series.points:
                backlog = int(series.points[0].value.int64_value)
                print(f"  Queue depth  : ~{backlog} message(s)")
                break
    except Exception:
        print( "  Queue depth  : (install google-cloud-monitoring to see this)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pause, resume, or check Aperture's processing state."
    )
    parser.add_argument(
        "command",
        choices=["pause", "resume", "status"],
        help="pause | resume | status",
    )
    args = parser.parse_args()

    if args.command == "pause":
        cmd_pause()
    elif args.command == "resume":
        cmd_resume()
    elif args.command == "status":
        cmd_status()


if __name__ == "__main__":
    main()
