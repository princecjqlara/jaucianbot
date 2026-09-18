import io
import json
import os
import unittest
from unittest.mock import patch

from app import app


def call_app(path, *, method="GET", headers=None, body=b"", query=""):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    for name, value in (headers or {}).items():
        environ["HTTP_" + name.upper().replace("-", "_")] = value
    captured = {}

    def start_response(status, response_headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(response_headers)

    payload = json.loads(b"".join(app(environ, start_response)))
    return captured["status"], payload


class WsgiApplicationTests(unittest.TestCase):
    def test_public_health_checks_supabase_without_exposing_groups(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-100123"}), patch(
            "app.archive_status", return_value=[{"chat_id": -100123}]
        ) as status_query:
            status, payload = call_app("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True})
        status_query.assert_called_once_with({-100123})

    def test_public_health_reports_database_failure(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-100123"}), patch(
            "app.archive_status", side_effect=RuntimeError("database unavailable")
        ):
            status, payload = call_app("/api/health")
        self.assertEqual(status, 503)
        self.assertEqual(payload, {"ok": False})

    def test_status_requires_authorization(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct"}):
            status, payload = call_app("/api/status")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_status_uses_cloud_archive(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"}), patch(
            "app.archive_status", return_value=[{"chat_id": -100123}]
        ):
            status, payload = call_app(
                "/api/status", headers={"Authorization": "Bearer correct"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["groups"], [{"chat_id": -100123}])

    def test_groups_show_discovered_and_approved_state(self):
        discovered = [{"chat_id": -100999, "title": "Daily Reports", "approved": False}]
        with patch.dict(
            os.environ,
            {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"},
        ), patch("app.known_chats", return_value=discovered) as query:
            status, payload = call_app(
                "/api/groups", headers={"Authorization": "Bearer correct"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["groups"], discovered)
        query.assert_called_once_with({-100123})

    def test_webhook_stores_valid_update(self):
        body = json.dumps({"update_id": 1}).encode()
        with patch.dict(
            os.environ,
            {"TELEGRAM_WEBHOOK_SECRET": "correct", "ALLOWED_CHAT_IDS": "-100123"},
        ), patch("app.save_update") as save:
            status, payload = call_app(
                "/api/webhook",
                method="POST",
                headers={"X-Telegram-Bot-Api-Secret-Token": "correct"},
                body=body,
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        save.assert_called_once_with({"update_id": 1}, {-100123})

    def test_schedules_require_authorization(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct"}):
            status, payload = call_app("/api/schedules")
        self.assertEqual(status, 401)
        self.assertFalse(payload["ok"])

    def test_creates_scheduled_message_for_approved_group(self):
        request_body = json.dumps({
            "chat_id": -100123,
            "action_type": "message",
            "scheduled_for": "2026-09-20T09:30:00+08:00",
            "payload": {"text": "Team update", "disable_notification": True},
        }).encode()
        created = {"id": 7, "chat_id": -100123, "status": "pending"}
        with patch.dict(
            os.environ,
            {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"},
        ), patch("app.create_scheduled_action", return_value=created) as create:
            status, payload = call_app(
                "/api/schedules",
                method="POST",
                headers={"Authorization": "Bearer correct"},
                body=request_body,
            )
        self.assertEqual(status, 201)
        self.assertEqual(payload["schedule"], created)
        create.assert_called_once()
        called = create.call_args.kwargs
        self.assertEqual(called["chat_id"], -100123)
        self.assertEqual(called["action_type"], "message")
        self.assertEqual(called["payload"]["text"], "Team update")
        self.assertEqual(called["scheduled_for"].isoformat(), "2026-09-20T01:30:00+00:00")

    def test_rejects_schedule_for_unapproved_group(self):
        request_body = json.dumps({
            "chat_id": -100999,
            "action_type": "message",
            "scheduled_for": "2026-09-20T09:30:00+08:00",
            "payload": {"text": "Team update"},
        }).encode()
        with patch.dict(
            os.environ,
            {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"},
        ), patch("app.create_scheduled_action") as create:
            status, _ = call_app(
                "/api/schedules",
                method="POST",
                headers={"Authorization": "Bearer correct"},
                body=request_body,
            )
        self.assertEqual(status, 403)
        create.assert_not_called()

    def test_dispatches_due_action_and_archives_sent_message(self):
        action = {
            "id": 7,
            "chat_id": -100123,
            "action_type": "message",
            "payload": {"text": "Team update"},
        }
        sent_message = {"message_id": 51, "chat": {"id": -100123, "type": "supergroup"}, "date": 1}
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-100123"}), patch(
            "app.claim_scheduled_actions", return_value=[action]
        ) as claim, patch("app.send_scheduled_action", return_value=sent_message) as send, patch(
            "app.finish_scheduled_action", return_value=True
        ) as finish, patch("app.save_update") as save:
            status, payload = call_app("/api/cron/dispatch")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "processed": 1, "sent": 1, "failed": 0})
        claim.assert_called_once_with({-100123}, limit=10)
        send.assert_called_once_with(action)
        finish.assert_called_once_with(7, success=True, telegram_message_id=51)
        save.assert_called_once_with({"message": sent_message}, {-100123})

    def test_dispatch_with_no_due_actions_is_safe(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-100123"}), patch(
            "app.claim_scheduled_actions", return_value=[]
        ):
            status, payload = call_app("/api/cron/dispatch")
        self.assertEqual(status, 200)
        self.assertEqual(payload["processed"], 0)

    def test_unknown_route_returns_not_found(self):
        status, _ = call_app("/missing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
