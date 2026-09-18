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

    def test_unknown_route_returns_not_found(self):
        status, _ = call_app("/missing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
