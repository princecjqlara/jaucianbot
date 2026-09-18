"""Authenticated archive coverage endpoint."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from cloud_http import authorized, send_json
from cloud_store import allowed_chat_ids, archive_status


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not authorized(self.headers.get("Authorization"), "INSIGHTS_API_KEY"):
            send_json(self, 401, {"ok": False})
            return
        try:
            allowed = allowed_chat_ids()
            groups = archive_status(allowed)
        except Exception as error:
            print(f"Status query failed: {type(error).__name__}")
            send_json(self, 503, {"ok": False})
            return
        send_json(self, 200, {"ok": True, "groups": groups})
