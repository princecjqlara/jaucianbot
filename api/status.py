"""Authenticated archive coverage endpoint."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from cloud_http import authorized, send_json
from cloud_store import allowed_chat_ids, connect, ensure_schema


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not authorized(self.headers.get("Authorization"), "INSIGHTS_API_KEY"):
            send_json(self, 401, {"ok": False})
            return
        try:
            allowed = allowed_chat_ids()
            with connect() as db:
                ensure_schema(db)
                groups = db.execute(
                    """
                    SELECT c.chat_id, c.title, c.membership,
                           COUNT(m.message_id) AS stored_messages,
                           COUNT(m.message_id) FILTER (WHERE m.source='bot') AS bot_messages,
                           COUNT(m.message_id) FILTER (WHERE m.source='export') AS export_messages,
                           MIN(m.sent_utc) AS first_message_utc,
                           MAX(m.sent_utc) AS last_message_utc
                    FROM chats c LEFT JOIN messages m USING(chat_id)
                    WHERE c.chat_id = ANY(%s)
                    GROUP BY c.chat_id ORDER BY c.title
                    """,
                    (list(allowed),),
                ).fetchall()
        except Exception as error:
            print(f"Status query failed: {type(error).__name__}")
            send_json(self, 503, {"ok": False})
            return
        send_json(self, 200, {"ok": True, "groups": groups})
