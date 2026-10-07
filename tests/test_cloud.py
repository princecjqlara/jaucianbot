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
    activity_messages, allowed_chat_ids, daily_messages, daily_poll_active_users, daily_poll_answers,
    existing_daily_reminder_chats, new_client_actions, new_client_reply_messages, save_update,
    poll_answers_for_range, update_new_client_action,
)


class CloudStorageTests(unittest.TestCase):
    def test_activity_queries_keep_both_time_bounds_and_paginate(self):
        start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=2)
        with patch("cloud_store.request", side_effect=[[{"message_id": i} for i in range(1000)], []]) as request:
            rows = activity_messages(-100123, start, end, {6, 7})
        self.assertEqual(len(rows), 1000)
        for index, call in enumerate(request.call_args_list):
            params = urllib.parse.parse_qs(call.args[0].split("?", 1)[1])
            self.assertEqual(params["sent_utc"], [f"gte.{start.isoformat()}", f"lt.{end.isoformat()}"])
            self.assertEqual(params["offset"], [str(index * 1000)])

    def test_poll_history_keeps_both_date_bounds_and_paginate(self):
        start, end = dt.date(2026, 10, 1), dt.date(2026, 10, 5)
        with patch("cloud_store.request", side_effect=[[{"user_id": i} for i in range(1000)], []]) as request:
            rows = poll_answers_for_range(-100123, start, end)
        self.assertEqual(len(rows), 1000)
        for index, call in enumerate(request.call_args_list):
            params = urllib.parse.parse_qs(call.args[0].split("?", 1)[1])
            self.assertEqual(params["daily_polls.work_date"], ["gte.2026-10-01", "lte.2026-10-05"])
            self.assertEqual(params["offset"], [str(index * 1000)])

    def test_daily_report_reads_earlier_deals_beyond_500_chat_messages(self):
        start = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
        with patch("cloud_store.request", side_effect=[[{"message_id": i} for i in range(501)], [{"message_id": 900}]]) as request:
            rows = daily_messages(-100123, start, start + dt.timedelta(days=1))
        self.assertEqual(len(rows), 502)
        self.assertEqual(rows[-1]["message_id"], 900)
        params = urllib.parse.parse_qs(request.call_args_list[1].args[0].split("?", 1)[1])
        self.assertEqual(params["offset"], ["501"])

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
            daily_poll_answers("poll-1")
        message_path = request.call_args_list[0].args[0]
        message_params = urllib.parse.parse_qs(message_path.split("?", 1)[1])
        self.assertIn("author_id", message_params["select"][0])
        self.assertEqual(message_params["sent_utc"], [f"gte.{start.isoformat()}", f"lt.{end.isoformat()}"])
        self.assertEqual(message_params["limit"], ["100"])
        answer_path = request.call_args_list[1].args[0]
        answer_params = urllib.parse.parse_qs(answer_path.split("?", 1)[1])
        self.assertEqual(answer_params["poll_id"], ["eq.poll-1"])
        self.assertEqual(answer_params["active"], ["eq.true"])
        self.assertIn("updated_at", answer_params["select"][0])
        all_answer_path = request.call_args_list[2].args[0]
        all_answer_params = urllib.parse.parse_qs(all_answer_path.split("?", 1)[1])
        self.assertNotIn("active", all_answer_params)

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

    def test_new_client_update_can_include_timed_out_cancelled_action(self):
        with patch("cloud_store.request", return_value=[]) as request:
            update_new_client_action(
                42, {"new_client_token": "ABCDEF12"}, status="cancelled",
                include_cancelled=True,
            )
        path = request.call_args.args[0]
        params = urllib.parse.parse_qs(path.split("?", 1)[1])
        self.assertEqual(params["id"], ["eq.42"])
        self.assertEqual(
            params["status"], ["in.(pending,processing,failed,cancelled)"],
        )


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
