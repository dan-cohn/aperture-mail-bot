#!/usr/bin/env bash
# =============================================================================
# Aperture — Cloud Run Deployment
#
# Run once (and on every code update):
#   chmod +x scripts/deploy.sh
#   ./scripts/deploy.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker running locally
#   - .env file populated (used to push secrets to Secret Manager)
# =============================================================================
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_ID="aperture-prod-20260221"
REGION="us-central1"
SERVICE_NAME="aperture"
REPO_NAME="aperture-repo"
IMAGE="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO_NAME/$SERVICE_NAME"

# --quick skips one-time setup (API enables, IAM grants, secret sync)
QUICK=false
if [[ "${1:-}" == "--quick" ]]; then
  QUICK=true
fi

echo "=== Aperture Deployment$([ "$QUICK" = "true" ] && echo " (quick mode)") ==="
echo "Project : $PROJECT_ID"
echo "Region  : $REGION"
echo "Image   : $IMAGE"
echo ""

if [ "$QUICK" = false ]; then
  # ── Step 1: Enable APIs ─────────────────────────────────────────────────────
  echo "--- Enabling required APIs..."
  gcloud services enable \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    --project="$PROJECT_ID"

  # ── Step 2: Create Artifact Registry repo (idempotent) ───────────────────────
  echo "--- Creating Artifact Registry repository (if not exists)..."
  gcloud artifacts repositories describe "$REPO_NAME" \
    --location="$REGION" --project="$PROJECT_ID" &>/dev/null || \
  gcloud artifacts repositories create "$REPO_NAME" \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT_ID"
fi

# ── Step 3: Configure Docker auth ────────────────────────────────────────────
echo "--- Configuring Docker authentication..."
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet

# ── Step 4: Build and push image (with registry cache) ───────────────────────
echo "--- Building Docker image..."
docker buildx build \
  --platform linux/amd64 \
  --cache-from "type=registry,ref=$IMAGE:latest" \
  --cache-to "type=inline" \
  -t "$IMAGE:latest" \
  --push \
  .

if [ "$QUICK" = false ]; then
  # ── Step 5: Push secrets to Secret Manager ───────────────────────────────────
  echo "--- Syncing secrets to Secret Manager..."

  push_secret() {
    local name="$1"
    local value="$2"
    if gcloud secrets describe "$name" --project="$PROJECT_ID" &>/dev/null; then
      printf '%s' "$value" | gcloud secrets versions add "$name" \
        --data-file=- --project="$PROJECT_ID"
    else
      printf '%s' "$value" | gcloud secrets create "$name" \
        --data-file=- --project="$PROJECT_ID" --replication-policy=automatic
    fi
  }

  # Load .env (skip comments and blank lines)
  if [ -f .env ]; then
    while IFS='=' read -r key value; do
      [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
      # Strip inline comments
      value="${value%%#*}"
      value="${value%"${value##*[![:space:]]}"}"  # rtrim
      case "$key" in
        TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|GEMINI_API_KEY|INTERNAL_SECRET|TELEGRAM_WEBHOOK_SECRET)
          push_secret "aperture-$key" "$value"
          ;;
      esac
    done < .env
  else
    echo "WARNING: .env not found — skipping secret sync"
  fi

  # ── Step 6: Grant Cloud Run SA access to secrets + Firestore ─────────────────
  echo "--- Granting service account permissions..."
  PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)")
  SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA" \
    --role="roles/datastore.user" --quiet

  for secret in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID GEMINI_API_KEY INTERNAL_SECRET TELEGRAM_WEBHOOK_SECRET; do
    gcloud secrets add-iam-policy-binding "aperture-$secret" \
      --member="serviceAccount:$SA" \
      --role="roles/secretmanager.secretAccessor" \
      --project="$PROJECT_ID" --quiet
  done
fi

# ── Step 6b: Sync prompts to Firestore (always runs) ─────────────────────────
echo "--- Syncing prompts to Firestore..."
.venv/bin/python scripts/sync_prompt.py

# ── Step 7: Deploy to Cloud Run ───────────────────────────────────────────────
echo "--- Deploying to Cloud Run..."
gcloud run deploy "$SERVICE_NAME" \
  --image="$IMAGE:latest" \
  --platform=managed \
  --region="$REGION" \
  --project="$PROJECT_ID" \
  --allow-unauthenticated \
  --min-instances=0 \
  --max-instances=2 \
  --memory=512Mi \
  --cpu=1 \
  --timeout=120 \
  --set-env-vars="GCP_PROJECT_ID=$PROJECT_ID,FIRESTORE_DATABASE=aperture-db,PUBSUB_TOPIC=aperture-gmail-push,PUBSUB_SUBSCRIPTION=aperture-gmail-push-sub,LLM_PROVIDER=gemini,GEMINI_MODEL=gemini-2.5-flash,TIMEZONE=America/Chicago,LOG_LEVEL=INFO,ENVIRONMENT=production" \
  --set-secrets="TELEGRAM_BOT_TOKEN=aperture-TELEGRAM_BOT_TOKEN:latest,TELEGRAM_CHAT_ID=aperture-TELEGRAM_CHAT_ID:latest,GEMINI_API_KEY=aperture-GEMINI_API_KEY:latest,INTERNAL_SECRET=aperture-INTERNAL_SECRET:latest,TELEGRAM_WEBHOOK_SECRET=aperture-TELEGRAM_WEBHOOK_SECRET:latest"

# ── Step 8: Print the service URL ─────────────────────────────────────────────
SERVICE_URL=$(gcloud run services describe "$SERVICE_NAME" \
  --region="$REGION" --project="$PROJECT_ID" \
  --format="value(status.url)")

# ── Step 9: Register Telegram webhook ────────────────────────────────────────
echo "--- Registering Telegram webhook..."
BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' .env | cut -d'=' -f2- | sed 's/#.*//' | xargs)
WEBHOOK_SECRET=$(grep '^TELEGRAM_WEBHOOK_SECRET=' .env | cut -d'=' -f2- | sed 's/#.*//' | xargs)

if [ -n "$BOT_TOKEN" ]; then
  WEBHOOK_URL="$SERVICE_URL/webhook/telegram"
  PAYLOAD="{\"url\": \"$WEBHOOK_URL\", \"allowed_updates\": [\"callback_query\"]"
  if [ -n "$WEBHOOK_SECRET" ]; then
    PAYLOAD="$PAYLOAD, \"secret_token\": \"$WEBHOOK_SECRET\""
  fi
  PAYLOAD="$PAYLOAD}"

  RESPONSE=$(curl -s -X POST \
    "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD")

  if echo "$RESPONSE" | grep -q '"ok":true'; then
    echo "Telegram webhook registered: $WEBHOOK_URL"
  else
    echo "WARNING: Telegram webhook registration failed: $RESPONSE"
  fi
else
  echo "WARNING: TELEGRAM_BOT_TOKEN not found in .env — skipping webhook registration"
fi

if [ "$QUICK" = false ]; then
  # ── Step 10: Create/update Cloud Scheduler jobs ──────────────────────────────
  echo "--- Configuring Cloud Scheduler jobs..."
  SCHEDULER_SA="$PROJECT_NUMBER-compute@developer.gserviceaccount.com"

  upsert_scheduler_job() {
    local job_name="$1"
    local schedule="$2"
    local uri="$3"
    local description="$4"
    local secret
    secret=$(grep '^INTERNAL_SECRET=' .env | cut -d'=' -f2- | sed 's/#.*//' | xargs)
    if gcloud scheduler jobs describe "$job_name" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
      gcloud scheduler jobs update http "$job_name" \
        --location="$REGION" --project="$PROJECT_ID" \
        --schedule="$schedule" --time-zone="America/Chicago" \
        --uri="$uri" --http-method=POST \
        --oidc-service-account-email="$SCHEDULER_SA" \
        --update-headers "X-Aperture-Secret=$secret" \
        --quiet
    else
      gcloud scheduler jobs create http "$job_name" \
        --location="$REGION" --project="$PROJECT_ID" \
        --description="$description" \
        --schedule="$schedule" --time-zone="America/Chicago" \
        --uri="$uri" --http-method=POST \
        --oidc-service-account-email="$SCHEDULER_SA" \
        --headers "X-Aperture-Secret=$secret" \
        --quiet
    fi
    echo "  ✓ $job_name ($schedule)"
  }

  upsert_scheduler_job "aperture-digest-morning" "30 7 * * *"  "$SERVICE_URL/internal/digest/morning" "Morning archive digest"
  upsert_scheduler_job "aperture-digest-evening" "30 17 * * *" "$SERVICE_URL/internal/digest/evening" "Evening inbox digest"
fi

echo ""
echo "=== Deployment Complete ==="
echo "Service URL: $SERVICE_URL"
echo ""
