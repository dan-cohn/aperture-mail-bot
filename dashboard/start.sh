#!/bin/bash
set -e

# Generate cloudflared ingress config from environment
cat > /tmp/cloudflared-config.yml << EOF
protocol: http2

ingress:
  - hostname: ${CLOUDFLARE_TUNNEL_HOSTNAME}
    service: http://localhost:${PORT:-8080}
  - service: http_status:404
EOF

# Start cloudflared tunnel in background — outbound to Cloudflare edge
cloudflared --config /tmp/cloudflared-config.yml tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &

# Streamlit is the foreground process — Cloud Run health-checks this port
exec streamlit run dashboard/app.py \
  --server.port "${PORT:-8080}" \
  --server.headless true \
  --server.address 0.0.0.0 \
  --browser.gatherUsageStats false
