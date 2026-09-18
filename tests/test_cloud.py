import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from unittest.mock import patch

from api.webhook import handler
from cloud_store import allowed_chat_ids, save_update


class CloudStorageTests(unittest.TestCase):
    def test_passes_allowlist_to_supabase_rpc(self):
        update = {"update_id": 1, "message": {"message_id": 12}}
        with patch("cloud_store.request", return_value=True) as request:
            self.assertTrue(save_update(update, {-100456, -100123}))
            request.assert_called_once_with(
                "rpc/insights_ingest_update",
                {"p_update": update, "p_allowed_ids": [-100456, -100123]},
            )

    def test_rejects_bad_allowlist_configuration(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "invalid"}):
            with self.assertRaises(RuntimeError):
                allowed_chat_ids()


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
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct", "ALLOWED_CHAT_IDS": "-100123"}), patch("api.webhook.save_update") as save:
            self.assertEqual(self.post("wrong"), 401)
            save.assert_not_called()

    def test_accepts_valid_secret(self):
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct", "ALLOWED_CHAT_IDS": "-100123"}), patch("api.webhook.save_update") as save:
            self.assertEqual(self.post("correct"), 200)
            save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
