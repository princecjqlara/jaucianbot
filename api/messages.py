"""Authenticated, read-only message search endpoint."""

from __future__ import annotations

import datetime as dt
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from cloud_http import authorized, send_json
from cloud_store import allowed_chat_ids, archive_messages


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not authorized(self.headers.get("Authorization"), "INSIGHTS_API_KEY"):
            send_json(self, 401, {"ok": False})
            return
        args = parse_qs(urlsplit(self.path).query)
        try:
            limit = int(args.get("limit", ["200"])[0])
            days = float(args.get("days", ["7"])[0])
            group = int(args["group"][0]) if "group" in args else None
            if not 1 <= limit <= 500 or not 0 <= days <= 3650:
                raise ValueError("out of range")
        except (ValueError, IndexError):
            send_json(self, 400, {"ok": False, "error": "invalid query"})
            return
        try:
            allowed = allowed_chat_ids()
            if group is not None and group not in allowed:
                send_json(self, 403, {"ok": False})
                return
            text = args.get("q", [""])[0]
            messages = archive_messages(
                allowed,
                since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days),
                limit=limit,
                group=group,
                query=text,
            )
        except Exception as error:
            print(f"Messages query failed: {type(error).__name__}")
            send_json(self, 503, {"ok": False})
            return
        send_json(self, 200, {"ok": True, "messages": messages})
