#!/usr/bin/env python3
"""
Aperture Dashboard Control Server

Runs on the dashboard VM alongside cloudflared. Exposes a tiny
authenticated HTTP API so the aperture Cloud Run service can start
and stop the cloudflared tunnel remotely.

Endpoints (all require X-Aperture-Secret header):
  POST /start   — systemctl start cloudflared
  POST /stop    — systemctl stop cloudflared
  GET  /status  — returns "active" or "inactive"

Environment:
  INTERNAL_SECRET   shared secret (same value as aperture's)
  CONTROL_PORT      port to listen on (default 9090)
"""
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ["INTERNAL_SECRET"]
PORT = int(os.environ.get("CONTROL_PORT", "9090"))


class Handler(BaseHTTPRequestHandler):
    # Drop connections that don't send a request within 10 s (scanner mitigation)
    timeout = 10
    def do_POST(self):
        if not self._authorized():
            return
        if self.path == "/start":
            subprocess.Popen(["systemctl", "start", "cloudflared"])
            self._reply(200, b"starting")
        elif self.path == "/stop":
            subprocess.Popen(["systemctl", "stop", "cloudflared"])
            self._reply(200, b"stopping")
        else:
            self._reply(404, b"not found")

    def do_GET(self):
        if not self._authorized():
            return
        if self.path == "/status":
            result = subprocess.run(
                ["systemctl", "is-active", "cloudflared"],
                capture_output=True, text=True, check=False,
            )
            self._reply(200, result.stdout.strip().encode())
        else:
            self._reply(404, b"not found")

    def _authorized(self) -> bool:
        if self.headers.get("X-Aperture-Secret") != SECRET:
            self._reply(403, b"forbidden")
            return False
        return True

    def _reply(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    print(f"Aperture control server listening on port {PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
