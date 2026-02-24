#!/usr/bin/env python3
"""
Register Aperture's Cloud Run URL as the Telegram bot webhook.

Run once after deployment (and again if the Cloud Run URL changes):
    python scripts/setup_telegram_webhook.py

Also prints the current webhook info so you can verify it's set correctly.
"""
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings


def main() -> None:
    if not settings.cloud_run_url:
        print("ERROR: CLOUD_RUN_URL is not set in .env")
        sys.exit(1)

    if not settings.telegram_webhook_secret:
        print(
            "WARNING: TELEGRAM_WEBHOOK_SECRET is not set in .env.\n"
            "  Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "  Add it to .env as TELEGRAM_WEBHOOK_SECRET=...\n"
            "  Then add it to Secret Manager and redeploy.\n"
            "  Proceeding without verification (not recommended for production).\n"
        )

    webhook_url = f"{settings.cloud_run_url.rstrip('/')}/webhook/telegram"

    payload: dict = {"url": webhook_url, "allowed_updates": ["callback_query"]}
    if settings.telegram_webhook_secret:
        payload["secret_token"] = settings.telegram_webhook_secret

    print(f"Registering webhook: {webhook_url}")
    response = httpx.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/setWebhook",
        json=payload,
    )
    data = response.json()

    if data.get("ok"):
        print("Webhook registered successfully.")
    else:
        print(f"Failed: {data}")
        sys.exit(1)

    # Print current webhook info to confirm
    info = httpx.get(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/getWebhookInfo"
    ).json()
    wi = info.get("result", {})
    print(f"\nWebhook info:")
    print(f"  URL              : {wi.get('url')}")
    print(f"  Pending updates  : {wi.get('pending_update_count', 0)}")
    print(f"  Last error       : {wi.get('last_error_message', 'none')}")


if __name__ == "__main__":
    main()
