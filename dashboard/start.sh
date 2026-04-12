#!/bin/bash
set -e

# Start cloudflared tunnel in background — outbound to Cloudflare edge
cloudflared --config /app/dashboard/cloudflared-config.yml tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &

# Streamlit is the foreground process — Cloud Run health-checks this port
exec streamlit run dashboard/app.py \
  --server.port "${PORT:-8080}" \
  --server.headless true \
  --server.address 0.0.0.0 \
  --browser.gatherUsageStats false
