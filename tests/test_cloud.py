import json
import os
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import datetime as dt
from http.server import HTTPServer
from unittest.mock import patch

from api.webhook import handler
from cloud_store import (
    allowed_chat_ids, daily_messages, daily_poll_active_users,
    existing_daily_reminder_chats, new_client_actions, new_client_reply_messages, save_update,
)


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

    def test_daily_report_reads_authors_and_active_poll_voters(self):
        start = dt.datetime(2026, 9, 18, 16, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
        with patch("cloud_store.request", return_value=[]) as request:
            daily_messages(-100123, start, end)
            daily_poll_active_users("poll-1")
        message_path = request.call_args_list[0].args[0]
        message_params = urllib.parse.parse_qs(message_path.split("?", 1)[1])
        self.assertIn("author_id", message_params["select"][0])
        self.assertEqual(message_params["sent_utc"], [f"gte.{start.isoformat()}", f"lt.{end.isoformat()}"])
        self.assertEqual(message_params["limit"], ["501"])
        answer_path = request.call_args_list[1].args[0]
        answer_params = urllib.parse.parse_qs(answer_path.split("?", 1)[1])
        self.assertEqual(answer_params["poll_id"], ["eq.poll-1"])
        self.assertEqual(answer_params["active"], ["eq.true"])

    def test_existing_reminder_lookup_is_scoped_to_slot_and_groups(self):
        with patch("cloud_store.request", return_value=[{"chat_id": -100123}]) as request:
            chats = existing_daily_reminder_chats(dt.date(2026, 9, 19), 10, {-100123, -100456})
        self.assertEqual(chats, {-100123})
        path = request.call_args.args[0]
        params = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(params["chat_id"], ["in.(-100456,-100123)"])
        self.assertEqual(params["dedupe_key"], ['like."daily-reminder:2026-09-19:10:*"'])

    def test_new_client_history_uses_its_own_dedupe_namespace(self):
        with patch("cloud_store.request", return_value=[]) as request:
            self.assertEqual(new_client_actions({-100123}), [])
        path = request.call_args.args[0]
        params = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(params["dedupe_key"], ["like.new-client:*"])

    def test_new_client_reply_lookup_is_scoped_to_topic_and_time(self):
        since = dt.datetime(2026, 9, 27, 7, 0, tzinfo=dt.timezone.utc)
        before = since + dt.timedelta(hours=1)
        with patch("cloud_store.request", return_value=[]) as request:
            self.assertEqual(new_client_reply_messages(-100123, 4180, since, before), [])
        path = request.call_args.args[0]
        params = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(params["chat_id"], ["eq.-100123"])
        self.assertEqual(params["thread_id"], ["eq.4180"])
        self.assertEqual(params["sent_utc"], [
            "gte.2026-09-27T07:00:00+00:00", "lte.2026-09-27T08:00:00+00:00",
        ])
        self.assertEqual(params["text"], ["ilike.*working*"])


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
