#!/usr/bin/env bash
# =============================================================================
# Aperture — Dashboard VM Setup
#
# Provisions an e2-micro VM to run cloudflared and the control server.
# Run this script directly on the VM as root (or with sudo).
#
# Usage:
#   sudo bash setup_vm.sh <dashboard-cloud-run-url> <tunnel-hostname>
#
# Example:
#   sudo bash setup_vm.sh \
#     https://aperture-dashboard-xxxx-uc.a.run.app \
#     dashboard.example.com
#
# After running:
#   1. Populate /etc/aperture/env (see template printed at the end)
#   2. Open TCP port 9090 in GCP firewall rules for the VM
#   3. Add DASHBOARD_VM_CONTROL_URL=http://<vm-external-ip>:9090 to aperture .env
#   4. Redeploy aperture with ./scripts/deploy.sh
# =============================================================================
set -euo pipefail

DASHBOARD_URL="${1:?Usage: setup_vm.sh <dashboard-cloud-run-url> <tunnel-hostname>}"
TUNNEL_HOSTNAME="${2:?Usage: setup_vm.sh <dashboard-cloud-run-url> <tunnel-hostname>}"
CONTROL_PORT=9090
INSTALL_DIR=/opt/aperture
ENV_FILE=/etc/aperture/env

# ── Install cloudflared ───────────────────────────────────────────────────────
echo "--- Installing cloudflared..."
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
dpkg -i /tmp/cloudflared.deb
rm /tmp/cloudflared.deb

# ── Write cloudflared ingress config ─────────────────────────────────────────
echo "--- Writing cloudflared config..."
mkdir -p /etc/cloudflared
cat > /etc/cloudflared/config.yml << EOF
ingress:
  - hostname: ${TUNNEL_HOSTNAME}
    service: ${DASHBOARD_URL}
    originRequest:
      httpHostHeader: $(echo "$DASHBOARD_URL" | sed 's|https://||')
  - service: http_status:404
EOF

# ── cloudflared systemd service (disabled — started only via control server) ──
echo "--- Creating cloudflared systemd service..."
cat > /etc/systemd/system/cloudflared.service << 'EOF'
[Unit]
Description=Cloudflare Tunnel for Aperture Dashboard
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/aperture/env
ExecStart=/usr/bin/cloudflared --config /etc/cloudflared/config.yml tunnel --no-autoupdate run --token ${CLOUDFLARE_TUNNEL_TOKEN}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl disable cloudflared   # only starts via control server, not on boot

# ── Install control server ────────────────────────────────────────────────────
echo "--- Installing control server..."
mkdir -p "$INSTALL_DIR"
cp "$(dirname "$0")/vm_control_server.py" "$INSTALL_DIR/vm_control_server.py"
chmod +x "$INSTALL_DIR/vm_control_server.py"

cat > /etc/systemd/system/aperture-control.service << EOF
[Unit]
Description=Aperture Dashboard Control Server
After=network.target

[Service]
EnvironmentFile=${ENV_FILE}
Environment=CONTROL_PORT=${CONTROL_PORT}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/vm_control_server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable aperture-control

# ── Create env file template ──────────────────────────────────────────────────
echo "--- Creating env file template..."
mkdir -p /etc/aperture
if [ ! -f "$ENV_FILE" ]; then
  cat > "$ENV_FILE" << 'EOF'
INTERNAL_SECRET=<paste-from-aperture-.env>
CLOUDFLARE_TUNNEL_TOKEN=<paste-from-aperture-.env>
EOF
  chmod 600 "$ENV_FILE"
  echo "  Created $ENV_FILE — fill in the two values before starting the service."
else
  echo "  $ENV_FILE already exists — skipping."
fi

echo ""
echo "=== VM setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $ENV_FILE and fill in INTERNAL_SECRET and CLOUDFLARE_TUNNEL_TOKEN"
echo "  2. Start the control server:  systemctl start aperture-control"
echo "  3. Verify it's running:       systemctl status aperture-control"
echo "  4. Open TCP port $CONTROL_PORT in GCP firewall rules for this VM's network tag"
echo "  5. Add to aperture .env:      DASHBOARD_VM_CONTROL_URL=http://<this-vm-external-ip>:$CONTROL_PORT"
echo "  6. Redeploy aperture:         ./scripts/deploy.sh"
