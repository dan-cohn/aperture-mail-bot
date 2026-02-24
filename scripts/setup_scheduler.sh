#!/usr/bin/env bash
# =============================================================================
# Aperture — Cloud Scheduler Job Setup
#
# Run once after deploy.sh:
#   chmod +x scripts/setup_scheduler.sh
#   ./scripts/setup_scheduler.sh https://YOUR-CLOUD-RUN-URL
#
# Timezone is read from TIMEZONE in .env (defaults to America/New_York).
# =============================================================================
set -euo pipefail

SERVICE_URL="${1:?Usage: $0 <cloud-run-url>}"
PROJECT_ID="aperture-prod-20260221"
REGION="us-central1"

# Read INTERNAL_SECRET and TIMEZONE from .env
INTERNAL_SECRET=""
TIMEZONE="America/New_York"  # default fallback
if [ -f .env ]; then
  INTERNAL_SECRET=$(grep '^INTERNAL_SECRET=' .env | cut -d'=' -f2- | sed 's/#.*//' | xargs)
  TZ_FROM_ENV=$(grep '^TIMEZONE=' .env | cut -d'=' -f2- | sed 's/#.*//' | xargs)
  [ -n "$TZ_FROM_ENV" ] && TIMEZONE="$TZ_FROM_ENV"
fi
if [ -z "$INTERNAL_SECRET" ]; then
  echo "ERROR: INTERNAL_SECRET not found in .env"
  exit 1
fi

echo "=== Creating Cloud Scheduler Jobs ==="
echo "Service URL : $SERVICE_URL"
echo "Project     : $PROJECT_ID"
echo "Timezone    : $TIMEZONE"
echo ""

create_or_update_job() {
  local name="$1"
  local schedule="$2"
  local uri="$3"
  local description="$4"

  if gcloud scheduler jobs describe "$name" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    echo "Updating existing job: $name"
    gcloud scheduler jobs update http "$name" \
      --location="$REGION" \
      --project="$PROJECT_ID" \
      --schedule="$schedule" \
      --uri="$uri" \
      --http-method=POST \
      --update-headers="X-Aperture-Secret=$INTERNAL_SECRET" \
      --time-zone="$TIMEZONE" \
      --attempt-deadline=60s \
      --quiet
  else
    echo "Creating job: $name ($description)"
    gcloud scheduler jobs create http "$name" \
      --location="$REGION" \
      --project="$PROJECT_ID" \
      --schedule="$schedule" \
      --uri="$uri" \
      --http-method=POST \
      --headers="X-Aperture-Secret=$INTERNAL_SECRET" \
      --time-zone="$TIMEZONE" \
      --attempt-deadline=60s \
      --description="$description"
  fi
}

# ── Daily digest — morning ────────────────────────────────────────────────────
create_or_update_job \
  "aperture-digest-morning" \
  "30 7 * * *" \
  "$SERVICE_URL/internal/digest" \
  "Aperture: morning digest (07:30)"

# ── Daily digest — evening ────────────────────────────────────────────────────
create_or_update_job \
  "aperture-digest-evening" \
  "30 17 * * *" \
  "$SERVICE_URL/internal/digest" \
  "Aperture: evening digest (17:30)"

# ── Weekly unsubscribe reminder — Sundays 10:00 ───────────────────────────────
create_or_update_job \
  "aperture-unsubscribe-reminder" \
  "0 10 * * 0" \
  "$SERVICE_URL/internal/unsubscribe-reminder" \
  "Aperture: weekly unsubscribe reminder (Sun 10:00)"

# ── Gmail watch renewal — every 5 days ───────────────────────────────────────
# Runs on days 1,6,11,16,21,26 — keeps a 2-day buffer before 7-day expiry
create_or_update_job \
  "aperture-renew-watch" \
  "0 6 */5 * *" \
  "$SERVICE_URL/internal/renew-watch" \
  "Aperture: renew Gmail watch (every 5 days)"

# ── Snooze processor — every 15 minutes ──────────────────────────────────────
create_or_update_job \
  "aperture-process-snoozes" \
  "*/15 * * * *" \
  "$SERVICE_URL/internal/process-snoozes" \
  "Aperture: re-fire expired snooze alerts (every 15 min)"

echo ""
echo "=== Scheduler Jobs Created ==="
gcloud scheduler jobs list --location="$REGION" --project="$PROJECT_ID" \
  --filter="name:aperture-"
