import datetime as dt
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import automation_controls as controls
import cloud_store
import database_scheduler
import app as application
from daily_automation import GROUPS, TRABAWHO_CHAT_ID
from telegram_sender import TelegramError, telegram_call
from test_wsgi import call_app


VEO = -1003647732254
APPROVED = set(GROUPS) | {TRABAWHO_CHAT_ID, -1004362432984}
SETTINGS = {"AUTOMATION_CHAT_IDS": str(TRABAWHO_CHAT_ID),
            "ALLOWED_CHAT_IDS": ",".join(map(str, sorted(APPROVED))),
            "INSIGHTS_API_KEY": "test-key", "TELEGRAM_WEBHOOK_SECRET": "test-secret"}


class AutomationControlsTests(unittest.TestCase):
    def test_old_task_reminder_stops_without_releasing_delivered_owner(self):
        from freebie_automation import retire_stale_delivery
        now = dt.datetime(2026, 10, 21, 16, 5, tzinfo=dt.timezone.utc)
        payload = {"freebie_token": "AABBCCDD", "freebie_assignee_id": 99,
            "freebie_assigned_at": "2026-10-18T03:00:00+00:00"}
        action = {"id": 7, "status": "processing", "payload": payload, "sent_at": "2026-10-18T03:01:00+00:00"}
        with patch("cloud_store._update_claim", return_value=True) as update:
            self.assertTrue(retire_stale_delivery(action, now))
        changes = update.call_args.args[1]
        self.assertEqual(changes["status"], "sent")
        self.assertIsNone(changes["repeat_interval_minutes"])
        self.assertEqual(changes["payload"]["freebie_assignee_id"], 99)
        self.assertNotIn("freebie_cancelled_at", changes["payload"])

    def test_unseen_previous_day_task_expires_but_current_philippine_day_does_not(self):
        from freebie_automation import retire_stale_delivery
        now = dt.datetime(2026, 10, 21, 16, 5, tzinfo=dt.timezone.utc)
        action = {"id": 7, "status": "processing", "payload": {
            "freebie_token": "AABBCCDD", "freebie_assignee_id": 99,
            "freebie_assigned_at": "2026-10-18T03:00:00+00:00"}}
        with patch("cloud_store._update_claim", return_value=True) as update:
            self.assertTrue(retire_stale_delivery(action, now))
            self.assertEqual(update.call_args.args[1]["status"], "cancelled")
            update.reset_mock()
            action["payload"]["freebie_assigned_at"] = "2026-10-21T16:00:00+00:00"
            self.assertIsNone(retire_stale_delivery(action, now))
            update.assert_not_called()

    def test_explicit_scope_keeps_only_approved_trabawho_active(self):
        with patch.dict(os.environ, SETTINGS, clear=True):
            self.assertEqual(controls.enabled_chat_ids(APPROVED), {TRABAWHO_CHAT_ID})
            self.assertEqual(cloud_store.allowed_chat_ids(), APPROVED)
            self.assertFalse(controls.chat_enabled(VEO))
            self.assertEqual(controls.enabled_chat_ids({VEO}), set())

    def test_absent_empty_and_invalid_settings_have_distinct_meanings(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(controls.enabled_chat_ids(APPROVED), APPROVED)
        with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": ""}, clear=True):
            self.assertEqual(controls.enabled_chat_ids(APPROVED), set())
        with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": "PRIVATE INVALID"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "must contain numeric chat IDs") as error:
                controls.enabled_chat_ids(APPROVED)
            self.assertNotIn("PRIVATE", str(error.exception))

    def test_claims_exclude_paused_pending_and_failed_actions(self):
        with patch.dict(os.environ, SETTINGS, clear=True), patch("cloud_store.request", return_value=[]) as request:
            cloud_store.claim_scheduled_actions(APPROVED)
            self.assertEqual(request.call_args.args[1]["p_allowed_ids"], [TRABAWHO_CHAT_ID])
            request.reset_mock()
            self.assertEqual(cloud_store.claim_scheduled_actions(set(GROUPS)), [])
            request.assert_not_called()

    def test_new_outgoing_work_is_not_created_for_paused_groups(self):
        with patch.dict(os.environ, SETTINGS, clear=True), patch("cloud_store.request") as request:
            self.assertFalse(cloud_store.enqueue_scheduled_action(chat_id=VEO, action_type="message",
                payload={"text": "test"}, scheduled_for=dt.datetime.now(dt.timezone.utc), dedupe_key="test"))
        request.assert_not_called()

    def test_raw_send_edit_and_poll_are_blocked_before_any_network_request(self):
        with patch.dict(os.environ, SETTINGS, clear=True), patch("telegram_sender.urllib.request.urlopen") as network:
            for method in ("sendMessage", "sendPoll", "editMessageText", "deleteMessage"):
                with self.subTest(method=method), self.assertRaisesRegex(TelegramError, "paused"):
                    telegram_call(method, {"chat_id": VEO})
        network.assert_not_called()

    def test_archiving_continues_but_paused_commands_do_not_change_readiness_or_ownership(self):
        update = {"message": {"message_id": 10, "chat": {"id": VEO}, "text": "/ready"}}
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.save_update", return_value=True) as archive, \
                patch("app.client_workflow.handle") as workflow, patch("app.ready_availability.handle") as ready, \
                patch("app.queue_new_client_assignments") as assignments:
            status, body = call_app("/api/webhook", method="POST", body=json.dumps(update).encode(),
                                   headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual((status, body), (200, {"ok": True}))
        archive.assert_called_once_with(update, APPROVED)
        workflow.assert_not_called()
        ready.assert_not_called()
        assignments.assert_not_called()

    def test_old_paused_buttons_do_not_acknowledge_or_change_assignments(self):
        update = {"callback_query": {"id": "callback", "data": "client:yes:token",
                                     "message": {"chat": {"id": VEO}}}}
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.client_workflow.handle") as workflow:
            status, body = call_app("/api/webhook", method="POST", body=json.dumps(update).encode(),
                                   headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual((status, body), (200, {"ok": True}))
        workflow.assert_not_called()

    def test_trabawho_commands_continue_to_reach_the_workflow(self):
        update = {"message": {"message_id": 10, "chat": {"id": TRABAWHO_CHAT_ID}, "text": "/clients"}}
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.save_update", return_value=True), \
                patch("app.client_workflow.handle", return_value="handled") as workflow:
            status, _ = call_app("/api/webhook", method="POST", body=json.dumps(update).encode(),
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual(status, 200)
        self.assertEqual(workflow.call_args.args[1], {TRABAWHO_CHAT_ID})

    def test_paused_poll_votes_are_archived_but_only_active_teams_are_checked(self):
        update = {"poll_answer": {"poll_id": "old-veo-poll"}}
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.save_poll_answer", return_value=True) as archive, \
                patch("app.client_workflow.handle", return_value=None) as workflow, \
                patch("app.ready_availability.handle", return_value=None), \
                patch("app.trabawho_poll_work_date", return_value=None) as context:
            status, _ = call_app("/api/webhook", method="POST", body=json.dumps(update).encode(),
                                 headers={"X-Telegram-Bot-Api-Secret-Token": "test-secret"})
        self.assertEqual(status, 200)
        archive.assert_called_once_with(update, APPROVED)
        self.assertEqual(workflow.call_args.args[1], {TRABAWHO_CHAT_ID})
        self.assertEqual(context.call_args.args[2], {TRABAWHO_CHAT_ID})

    def test_claimed_paused_work_is_deferred_without_finalizing_or_sending_it(self):
        action = {"id": 1, "chat_id": VEO, "status": "processing", "payload": {"text": "queued"}}
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.defer_scheduled_action", return_value=True) as defer, \
                patch("app.send_scheduled_action") as send, patch("app.finish_scheduled_action") as finish:
            self.assertEqual(application._deliver_claimed_actions([action], APPROVED), (1, 0, 0))
        defer.assert_called_once_with(action)
        send.assert_not_called()
        finish.assert_not_called()

    def test_paused_delivery_scope_does_not_claim_any_actions(self):
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.claim_scheduled_actions") as claim:
            self.assertEqual(application.deliver_due_actions(set(GROUPS)), (0, 0, 0))
        claim.assert_not_called()

    def test_status_reports_scope_while_preserving_all_archived_groups(self):
        with patch.dict(os.environ, SETTINGS, clear=True), patch("app.archive_status", return_value=[{"chat_id": VEO}]) as archive:
            status, body = call_app("/api/status", headers={"Authorization": "Bearer test-key"})
        self.assertEqual(status, 200)
        self.assertEqual(body["groups"], [{"chat_id": VEO}])
        self.assertEqual(body["automation"]["active_chat_ids"], [TRABAWHO_CHAT_ID])
        self.assertTrue(set(GROUPS).issubset(body["automation"]["paused_chat_ids"]))
        archive.assert_called_once_with(APPROVED)

    def test_scheduler_health_checks_active_delivery_queue_only(self):
        conn = MagicMock()
        conn.execute.side_effect = [
            MagicMock(fetchall=lambda: [("pg_cron", "1.6"), ("pg_net", "0.2")]),
            MagicMock(fetchone=lambda: (1, "* * * * *", True)),
            MagicMock(fetchone=lambda: None), MagicMock(fetchone=lambda: None),
            MagicMock(fetchone=lambda: None), MagicMock(fetchone=lambda: (0, 0, 0)),
        ]
        with patch.dict(os.environ, SETTINGS, clear=True):
            state = database_scheduler._snapshot(conn, APPROVED)
        self.assertEqual(conn.execute.call_args.args[1], ([TRABAWHO_CHAT_ID],))
        self.assertEqual(state["queue"], {"overdue_over_five_minutes": 0, "failed": 0, "stale_claims": 0})

    def test_trabawho_scope_does_not_disable_detection_of_real_queue_failures(self):
        state = {"configured": True, "queue": {"failed": 1}}
        self.assertIn("failed_deliveries", database_scheduler.health_reasons(state))
