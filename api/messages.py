"""Authenticated, read-only message search endpoint."""

from __future__ import annotations

import datetime as dt
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from cloud_http import authorized, send_json
from cloud_store import allowed_chat_ids, connect, ensure_schema


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
            clauses = ["m.chat_id = ANY(%s)", "m.sent_utc >= %s"]
            params: list = [list(allowed), dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)]
            if group is not None:
                clauses.append("m.chat_id = %s")
                params.append(group)
            text = args.get("q", [""])[0]
            if text:
                escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                clauses.append("m.text ILIKE %s ESCAPE '\\'")
                params.append(f"%{escaped}%")
            params.append(limit)
            query = """
                SELECT m.chat_id, c.title AS group_title, m.message_id, m.sent_utc,
                       m.edited_utc, m.author_name, m.text, m.content_type,
                       m.reply_to_message_id, m.thread_id, m.source
                FROM messages m JOIN chats c USING(chat_id)
                WHERE """ + " AND ".join(clauses) + " ORDER BY m.sent_utc DESC, m.message_id DESC LIMIT %s"
            with connect() as db:
                ensure_schema(db)
                messages = db.execute(query, tuple(params)).fetchall()
        except Exception as error:
            print(f"Messages query failed: {type(error).__name__}")
            send_json(self, 503, {"ok": False})
            return
        send_json(self, 200, {"ok": True, "messages": messages})
