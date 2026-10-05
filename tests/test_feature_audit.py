import datetime as dt
import html
import io
import json
import os
import re
import unittest
import urllib.parse
from unittest.mock import patch

import cloud_store
import crm_store
import new_client_automation
from app import app, deliver_due_actions
from new_client_automation import _detail_lines


def call(path, *, body=None, header="Bearer correct", method="GET"):
    raw = json.dumps(body).encode() if body is not None else b""
    env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(raw)),
           "wsgi.input": io.BytesIO(raw), "HTTP_AUTHORIZATION": header}
    captured = {}
    result = app(env, lambda status, headers: captured.update(status=int(status.split()[0])))
    return captured["status"], json.loads(b"".join(result))


class FeatureAuditTests(unittest.TestCase):
    def test_new_client_status_works_with_deployed_contact_identity_helpers(self):
        with patch("new_client_automation.new_client_actions", return_value=[]), patch(
            "new_client_automation._active_members", return_value=[]
        ), patch("new_client_automation._page_contacts", return_value=[("Onset Media Agency", [])]):
            groups = new_client_automation.new_client_status({-1004461399292}, dt.datetime(2026, 10, 5, tzinfo=dt.timezone.utc))
        self.assertEqual(groups[0]["available_complete_clients"], {"Onset Media Agency": 0})

    def setUp(self):
        archive = patch("new_client_automation.new_client_reply_messages", return_value=[])
        archive.start()
        self.addCleanup(archive.stop)

    def assignment(self, *, sent_at=None, first_at=None, work_date="2026-10-05", acknowledged_at=None):
        return {"id": 1, "chat_id": -1004461399292, "status": "pending", "sent_at": sent_at,
                "payload": {"new_client_token": "ABCDEF12", "new_client_assignee_id": 1,
                            "new_client_contact_id": "contact-1", "new_client_thread_id": 4180,
                            "new_client_page": "Onset Media Agency", "new_client_assignee_name": "Alex",
                            "new_client_assigned_at": "2026-10-05T00:00:00+00:00",
                            "new_client_work_date": work_date, "_first_delivery_at": first_at,
                            "new_client_acknowledged_at": acknowledged_at}}

    def test_queued_assignment_does_not_timeout_before_first_delivery(self):
        claim = self.assignment()
        now = dt.datetime(2026, 10, 5, 2, tzinfo=dt.timezone.utc)
        with patch("new_client_automation.update_new_client_action") as update:
            new_client_automation._release_stale_assignments([claim], now)
        update.assert_not_called()

    def test_one_hour_begins_at_delivery_and_is_not_extended_by_reminders(self):
        now = dt.datetime(2026, 10, 5, 2, tzinfo=dt.timezone.utc)
        fresh = self.assignment(sent_at="2026-10-05T01:30:00+00:00")
        stale = self.assignment(sent_at="2026-10-05T01:30:00+00:00", first_at="2026-10-05T00:30:00+00:00")
        with patch("new_client_automation.update_new_client_action", return_value={"id": 1}) as update:
            new_client_automation._release_stale_assignments([fresh], now)
            update.assert_not_called()
            new_client_automation._release_stale_assignments([stale], now)
        self.assertEqual(stale["status"], "cancelled")
        self.assertEqual(stale["payload"]["new_client_cancelled_reason"], "working_not_confirmed_within_one_hour")

    def test_unsent_prior_day_assignment_expires_and_does_not_reserve_client_forever(self):
        claim = self.assignment(work_date="2026-10-04")
        with patch("new_client_automation.update_new_client_action", return_value={"id": 1}):
            new_client_automation._release_stale_assignments([claim], dt.datetime(2026, 10, 5, 2, tzinfo=dt.timezone.utc))
        self.assertEqual(claim["payload"]["new_client_cancelled_reason"], "assignment_day_ended")
        reserve = getattr(new_client_automation, "_reserved_contact_keys", None) or new_client_automation._reserved_contact_ids
        self.assertEqual(reserve([claim]), set())
        claim["payload"]["new_client_acknowledged_at"] = "2026-10-05T01:00:00+00:00"
        self.assertTrue(reserve([claim]))

    def test_acknowledgment_report_uses_actual_confirmation_day(self):
        claim = self.assignment(work_date="2026-10-04", acknowledged_at="2026-10-04T17:00:00+00:00")
        with patch("new_client_automation.new_client_actions", return_value=[claim]):
            old = new_client_automation.new_client_report_lines(-1004461399292, dt.date(2026, 10, 4))
            today = new_client_automation.new_client_report_lines(-1004461399292, dt.date(2026, 10, 5))
        self.assertIn("Team total: 0", old)
        self.assertIn("Team total: 1", today)

    def test_first_delivery_timestamp_is_kept_across_reminders(self):
        claim = {"id": 1, "payload": {"_first_delivery_at": "2026-10-05T00:00:00+00:00"},
                 "repeat_interval_minutes": 60}
        with patch("cloud_store.request", return_value=[{"id": 1}]) as request:
            cloud_store.finish_scheduled_action(1, success=True, telegram_message_id=100, claim=claim)
        self.assertEqual(request.call_args.args[1]["payload"]["_first_delivery_at"], "2026-10-05T00:00:00+00:00")

    def test_suppressed_reminder_preserves_delivery_history(self):
        claim = {"id": 1, "updated_at": "2026-10-05T09:00:00+00:00", "attempts": 120,
                 "sent_at": "2026-10-05T06:00:00+00:00", "telegram_message_id": 42,
                 "repeat_interval_minutes": 180, "payload": {"text": "Task"}}
        with patch("cloud_store.request", return_value=[{"id": 1}]) as request:
            self.assertTrue(cloud_store.defer_scheduled_action(claim))
        changes = request.call_args.args[1]
        self.assertNotIn("sent_at", changes)
        self.assertNotIn("telegram_message_id", changes)
        self.assertEqual(changes["status"], "pending")
        params = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(params["updated_at"], ["eq." + claim["updated_at"]])

    def test_skipped_assignment_is_never_finalized_as_sent(self):
        claim = {"id": 1, "chat_id": -100123, "payload": {"freebie_token": "ABCDEF12"}}
        with patch("app.claim_scheduled_actions", return_value=[claim]), patch(
            "app.freebie_delivery_allowed", return_value=False
        ), patch("app.defer_scheduled_action", return_value=True) as defer, patch(
            "app.finish_scheduled_action"
        ) as finish, patch("app.send_scheduled_action") as send:
            self.assertEqual(deliver_due_actions({-100123}), (1, 0, 0))
        defer.assert_called_once_with(claim)
        finish.assert_not_called()
        send.assert_not_called()

    def test_delivery_errors_use_consecutive_failures_after_many_successes(self):
        claim = {"id": 1, "updated_at": "2026-10-05T09:00:00+00:00", "attempts": 132,
                 "repeat_interval_minutes": 180, "payload": {"text": "Task"}}
        with patch("cloud_store.request", return_value=[{"id": 1}]) as request:
            cloud_store.finish_scheduled_action(1, success=False, error="Network", claim=claim)
        changes = request.call_args.args[1]
        self.assertEqual(changes["status"], "pending")
        self.assertEqual(changes["attempts"], 1)
        self.assertEqual(changes["payload"]["_delivery_failures"], 1)

    def test_fifth_consecutive_failure_stops_retrying_and_success_resets_streak(self):
        claim = {"id": 1, "payload": {"_delivery_failures": 4}, "repeat_interval_minutes": 180}
        with patch("cloud_store.request", return_value=[{"id": 1}]) as request:
            cloud_store.finish_scheduled_action(1, success=False, claim=claim)
            self.assertEqual(request.call_args.args[1]["status"], "failed")
            cloud_store.finish_scheduled_action(1, success=True, telegram_message_id=100, claim=claim)
        changes = request.call_args.args[1]
        self.assertEqual(changes["payload"]["_delivery_failures"], 0)
        self.assertEqual(changes["attempts"], 0)
        self.assertEqual(changes["telegram_message_id"], 100)

    def test_stale_dispatcher_cannot_finish_a_reclaimed_or_cancelled_action(self):
        claim = {"id": 1, "updated_at": "2026-10-05T09:00:00+00:00", "payload": {}}
        with patch("cloud_store.request", return_value=[]) as request:
            self.assertFalse(cloud_store.finish_scheduled_action(1, success=True, claim=claim))
        params = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(params["status"], ["eq.processing"])
        self.assertEqual(params["updated_at"], ["eq." + claim["updated_at"]])

    def test_daily_queue_failure_does_not_stop_existing_due_deliveries(self):
        with patch.dict(os.environ, {"ALLOWED_CHAT_IDS": "-100123", "CRM_SUPABASE_SERVICE_ROLE_KEY": ""}), patch(
            "app.run_due_daily_automation", side_effect=RuntimeError("Unavailable")
        ), patch("app.deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, body = call("/api/cron/dispatch")
        deliver.assert_called_once_with({-100123}, limit=25)
        self.assertEqual(body["sent"], 1)
        self.assertEqual(status, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["queue_errors"], ["daily"])

    def test_complete_client_reader_paginates_past_first_1000(self):
        def row(i):
            return {"contact_id": str(i), "collected_details": {"business": "Studio"},
                    "contacts": {"id": str(i), "page_id": "page", "name": "Client", "psid": str(i)}}
        with patch("crm_store.crm_request", side_effect=[[row(i) for i in range(1000)], [row(1000)]]) as request:
            contacts = crm_store.completed_detail_contacts("page")
        self.assertEqual(len(contacts), 1001)
        for offset, entry in zip((0, 1000), request.call_args_list):
            query = urllib.parse.parse_qs(entry.args[0].split("?", 1)[1])
            self.assertEqual(query["offset"], [str(offset)])
            self.assertEqual(query["order"], ["contact_id.asc"])

    def test_complete_client_reader_rejects_nonobject_details(self):
        rows = [{"collected_details": value, "contacts": {"id": "1", "page_id": "page",
                 "name": "Client", "psid": "1"}} for value in ("incomplete", ["name"], True)]
        with patch("crm_store.crm_request", return_value=rows):
            self.assertEqual(crm_store.completed_detail_contacts("page"), [])

    def test_long_details_are_trimmed_before_html_escaping(self):
        details = {"x": "<>&\"" * 1000}
        rendered = _detail_lines({"collected_details": details})
        self.assertFalse(re.search(r"&(?!lt;|gt;|amp;|quot;|#x27;)", rendered))
        plain = html.unescape(rendered)
        self.assertEqual(plain, ("\u2022 x: " + details["x"])[:2800] + "\u2026")

    def test_invalid_nonascii_authorization_returns_401(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct"}):
            status, _ = call("/api/status", header="Bearer caf\u00e9")
        self.assertEqual(status, 401)

    def test_invalid_nonascii_webhook_secret_returns_401(self):
        env = {"PATH_INFO": "/api/webhook", "REQUEST_METHOD": "POST",
               "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": "caf\u00e9"}
        captured = {}
        with patch.dict(os.environ, {"TELEGRAM_WEBHOOK_SECRET": "correct"}):
            result = app(env, lambda status, headers: captured.update(status=int(status.split()[0])))
        self.assertEqual(captured["status"], 401)
        self.assertEqual(json.loads(b"".join(result)), {"ok": False})

    def test_list_action_type_is_a_bad_request_instead_of_a_server_failure(self):
        body = {"chat_id": -100123, "action_type": [], "payload": {"text": "Update"},
                "scheduled_for": "2026-10-05T09:00:00+00:00"}
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "correct", "ALLOWED_CHAT_IDS": "-100123"}):
            status, _ = call("/api/schedules", body=body, method="POST")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
