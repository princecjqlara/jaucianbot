import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

from api.webhook import handler
from cloud_store import save_update


class FakeConnection:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params):
        self.statements.append((sql, params))


class CloudStorageTests(unittest.TestCase):
    def test_saves_only_allowlisted_group_messages(self):
        db = FakeConnection()
        message = {
            "message_id": 12,
            "date": 1780000000,
            "chat": {"id": -100123, "type": "supergroup", "title": "Ops"},
            "from": {"id": 7, "first_name": "Alex"},
            "text": "New update",
        }
        self.assertFalse(save_update(db, {"message": message}, {-100456}))
        self.assertEqual(db.statements, [])
        self.assertTrue(save_update(db, {"message": message}, {-100123}))
        self.assertEqual(len(db.statements), 2)
        self.assertEqual(db.statements[1][1][5], "Alex")


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/webhook"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, secret):
        body = json.dumps({"update_id": 1, "message": {"chat": {"id": -100123, "type": "supergroup"}}}).encode()
        request = urllib.request.Request(self.url, data=body, headers={"X-Telegram-Bot-Api-Secret-Token": secret})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status
        except urllib.error.HTTPError as error:
            with error:
                return error.code

    def test_rejects_wrong_secret_before_storage(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct", "ALLOWED_CHAT_IDS": "-100123"}), patch("api.webhook.connect") as connect:
            self.assertEqual(self.post("wrong"), 401)
            connect.assert_not_called()

    def test_accepts_valid_secret(self):
        class Context:
            def __enter__(self):
                return object()

            def __exit__(self, *_):
                return False

        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct", "ALLOWED_CHAT_IDS": "-100123"}), patch("api.webhook.connect", return_value=Context()), patch("api.webhook.ensure_schema"), patch("api.webhook.save_update") as save:
            self.assertEqual(self.post("correct"), 200)
            save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
