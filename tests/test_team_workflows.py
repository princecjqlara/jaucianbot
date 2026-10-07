import datetime as dt
import io
import json
import unittest
from unittest.mock import patch

import remote_query
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID, TRABAWHO_CHAT_ID
from new_client_automation import _assignment_text
from test_wsgi import call_app


VEO = -1003647732254
NOW = dt.datetime(2026, 10, 8, 2, tzinfo=dt.timezone.utc)


class TeamWorkflowTests(unittest.TestCase):
    def test_mixed_dispatch_preserves_all_veo_workflows_and_adds_trabawho(self):
        allowed = set(GROUPS) | {TRABAWHO_CHAT_ID, DAILY_REPORTS_CHAT_ID}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": ",".join(map(str, allowed)), "CRM_SUPABASE_SERVICE_ROLE_KEY": "test"}, clear=True), patch(
            "app.run_due_daily_automation", return_value=1
        ) as daily, patch("app.queue_trabawho_automation", return_value=1) as trab, patch(
            "app.claim_automation_slot", return_value=True
        ), patch("app.queue_song_followups", return_value=1), patch("app.queue_trabawho_daily_report", return_value=1), patch(
            "app.suno_configured", return_value=True
        ), patch("app.queue_new_client_assignments", return_value=1) as clients, patch(
            "app.queue_freebie_assignments", return_value=1
        ) as freebies, patch("app.deliver_due_actions", return_value=(7, 7, 0)) as deliver:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 200)
        self.assertEqual(daily.call_args.args[1], allowed - {TRABAWHO_CHAT_ID})
        self.assertEqual(clients.call_args_list[0].args[1], allowed - {TRABAWHO_CHAT_ID})
        self.assertEqual(clients.call_args_list[1].args[1], {TRABAWHO_CHAT_ID})
        self.assertEqual(freebies.call_args.args[1], allowed)
        self.assertEqual(trab.call_args.args[1], allowed)
        deliver.assert_called_once_with(allowed, limit=25)
        self.assertEqual(body["sent"], 7)

    def test_trabawho_planning_failure_does_not_stop_veo_clients_freebies_or_delivery(self):
        allowed = {VEO, TRABAWHO_CHAT_ID}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": ",".join(map(str, allowed)), "CRM_SUPABASE_SERVICE_ROLE_KEY": "test"}, clear=True), patch(
            "app.run_due_daily_automation", return_value=1
        ), patch("app.queue_trabawho_automation", side_effect=RuntimeError), patch(
            "app.claim_automation_slot", return_value=True
        ), patch("app.queue_song_followups", return_value=0), patch("app.queue_trabawho_daily_report", return_value=0), patch(
            "app.suno_configured", return_value=False
        ), patch("app.queue_new_client_assignments", return_value=1) as clients, patch(
            "app.queue_freebie_assignments", return_value=1
        ) as freebies, patch("app.deliver_due_actions", return_value=(3, 3, 0)):
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["trabawho_plan"])
        clients.assert_called_once()
        freebies.assert_called_once()
        self.assertEqual(body["sent"], 3)

    def test_trabawho_poll_lookup_failure_does_not_swallow_veo_poll_webhook(self):
        update = {"poll_answer": {"poll_id": "veo-poll", "user": {"id": 7}, "option_ids": [0]}}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": f"{VEO},{TRABAWHO_CHAT_ID}",
                        "CRM_SUPABASE_SERVICE_ROLE_KEY": "test", "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch(
            "app.save_poll_answer", return_value=True
        ), patch("app.trabawho_poll_work_date", side_effect=RuntimeError), patch(
            "app.poll_answer_chat_today", return_value=VEO
        ), patch("app.queue_freebie_assignments", return_value=1) as freebies, patch(
            "app.queue_new_client_assignments", return_value=1
        ) as clients, patch("app.deliver_due_actions", return_value=(2, 2, 0)) as deliver:
            status, _ = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"},
                                 body=json.dumps(update).encode())
        self.assertEqual(status, 200)
        self.assertEqual(clients.call_args.args[1], {VEO})
        self.assertEqual(freebies.call_args.args[1], {VEO})
        deliver.assert_called_once_with({VEO}, limit=5)

    def test_song_delivery_instructions_added_only_to_trabawho(self):
        user = {"user_id": 7, "user_name": "Alex"}
        contact = {"id": "client", "name": "Client", "collected_details": {"request": "song"}}
        for chat in GROUPS:
            text, reminder = _assignment_text(user, "Page", contact, "ABCD1234", 1, chat_id=chat)
            self.assertNotIn("SONG SENT", text)
            self.assertNotIn("2894511895/7692", reminder)
        text, _ = _assignment_text(user, "Suno", contact, "ABCD1234", 1, chat_id=TRABAWHO_CHAT_ID)
        self.assertIn("SONG SENT ABCD1234", text)

    def test_trabawho_assignment_identifies_suno_page_separately_from_veo_pages(self):
        user = {"user_id": 7, "user_name": "Alex"}
        contact = {"id": "client", "name": "Client", "collected_details": {"request": "song"}}
        text, _ = _assignment_text(user, "Azshinari", contact, "ABCD1234", 1, chat_id=TRABAWHO_CHAT_ID)
        self.assertIn("Suno page: Azshinari", text)
        self.assertNotIn("Page: Azshinari", text)

    def test_startup_cli_uses_existing_shared_dispatcher(self):
        response = io.BytesIO(b'{"ok":true,"sent":1}')
        with patch.dict("os.environ", {"INSIGHTS_BASE_URL": "https://example.invalid", "INSIGHTS_API_KEY": "test"}, clear=True), patch(
            "sys.argv", ["remote_query.py", "dispatch"]
        ), patch("urllib.request.urlopen") as opened, patch("sys.stdout", new_callable=io.StringIO):
            opened.return_value.__enter__.return_value = response
            remote_query.main()
        self.assertEqual(opened.call_args.args[0].full_url, "https://example.invalid/api/cron/dispatch")
        self.assertEqual(opened.call_args.args[0].get_method(), "GET")
