#!/usr/bin/env python3
"""
Sync Aperture triage prompts to Firestore.

The core prompt is always synced from the local .prompt file.
The learned prompt is a freeform text block for rules that don't fit the
per-email correction format — editable without a redeploy.

Usage:
  python scripts/sync_prompt.py                       # sync core from .prompt
  python scripts/sync_prompt.py --learned FILE        # update learned prompt (archives current)
  python scripts/sync_prompt.py --show                # print current prompts from Firestore
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def _get_db():
    from google.cloud import firestore
    return firestore.Client(
        project=settings.gcp_project_id,
        database=settings.firestore_database,
    )


def sync_core(db, prompt_path: Path) -> None:
    if not prompt_path.exists():
        print(f"ERROR: .prompt file not found at {prompt_path}")
        sys.exit(1)
    content = prompt_path.read_text().strip()
    db.collection("aperture_config").document("prompt_core").set({
        "content": content,
        "synced_at": datetime.now(timezone.utc),
    })
    print(f"Core prompt synced ({len(content):,} chars).")


def sync_learned(db, content: str) -> None:
    """Update learned prompt, archiving the current version first."""
    ref = db.collection("aperture_config").document("prompt_learned")
    current = ref.get()

    if current.exists:
        data = current.to_dict()
        old_version = data.get("version", 1)
        db.collection("aperture_prompt_history").add({
            "content": data.get("content", ""),
            "version": old_version,
            "archived_at": datetime.now(timezone.utc),
        })
        new_version = old_version + 1
        print(f"Archived learned prompt v{old_version} to history.")
    else:
        new_version = 1

    ref.set({
        "content": content,
        "version": new_version,
        "updated_at": datetime.now(timezone.utc),
    })
    print(f"Learned prompt updated to v{new_version} ({len(content):,} chars).")


def init_learned_if_missing(db) -> None:
    ref = db.collection("aperture_config").document("prompt_learned")
    if not ref.get().exists:
        ref.set({
            "content": "",
            "version": 1,
            "updated_at": datetime.now(timezone.utc),
        })
        print("Learned prompt initialized (empty, v1).")


def show_prompts(db) -> None:
    core = db.collection("aperture_config").document("prompt_core").get()
    learned = db.collection("aperture_config").document("prompt_learned").get()

    print("=== Core Prompt ===")
    if core.exists:
        d = core.to_dict()
        print(f"Synced: {d.get('synced_at')}  |  {len(d.get('content', '')):,} chars")
        print(d.get("content", ""))
    else:
        print("(not found)")

    print("\n=== Learned Prompt ===")
    if learned.exists:
        d = learned.to_dict()
        print(f"v{d.get('version')}  |  Updated: {d.get('updated_at')}  |  {len(d.get('content', '')):,} chars")
        print(d.get("content", "") or "(empty)")
    else:
        print("(not found)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Aperture prompts to Firestore")
    parser.add_argument(
        "--learned", metavar="FILE",
        help="Update the learned prompt from FILE (archives the current version)",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print current prompts from Firestore and exit",
    )
    args = parser.parse_args()

    db = _get_db()
    prompt_path = Path(__file__).resolve().parent.parent / ".prompt"

    if args.show:
        show_prompts(db)
        return

    sync_core(db, prompt_path)

    if args.learned:
        learned_path = Path(args.learned)
        if not learned_path.exists():
            print(f"ERROR: File not found: {learned_path}")
            sys.exit(1)
        sync_learned(db, learned_path.read_text().strip())
    else:
        init_learned_if_missing(db)


if __name__ == "__main__":
    main()
