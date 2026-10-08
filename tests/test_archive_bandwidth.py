import datetime as dt
import unittest
import urllib.parse
from unittest.mock import patch

import cloud_store
from app import app
from test_wsgi import call_app
from daily_automation import DAILY_REPORTS_CHAT_ID, queue_daily_closeout


CHAT = -1004461399292
NOW = dt.datetime(2026, 10, 7, 10, 0, tzinfo=dt.timezone.utc)


class ArchiveBandwidthTests(unittest.TestCase):
    def test_history_keeps_stable_identities_and_timeouts_without_message_bodies(self):
        payload = {"new_client_token": "ABCD1234", "new_client_assignee_id": 22,
                   "new_client_contact_identity": "psid:page:123", "new_client_acknowledged_at": None,
                   "_first_delivery_at": NOW.isoformat()}
        row = {"id": 1, "chat_id": CHAT, "status": "pending"}
        row.update({f"p{i}": payload.get(key) for i, key in enumerate(cloud_store.NEW_CLIENT_HISTORY_KEYS)})
        with patch("cloud_store.request", return_value=[row]) as request:
            result = cloud_store.new_client_actions({CHAT})
        self.assertEqual(result[0]["payload"]["new_client_assignee_id"], 22)
        self.assertEqual(result[0]["payload"]["new_client_contact_identity"], "psid:page:123")
        self.assertEqual(result[0]["payload"]["_first_delivery_at"], NOW.isoformat())
        self.assertNotIn("new_client_acknowledged_at", result[0]["payload"])
        self.assertFalse(any(key.startswith("p") for key in result[0] if key != "payload"))
        selection = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])["select"][0]
        self.assertNotIn("payload->text", selection)
        self.assertNotIn("collected_details", selection)
        self.assertNotIn("reminder_text", selection)

    def test_pagination_does_not_drop_history_after_first_thousand(self):
        with patch("cloud_store.request", side_effect=[
            [{"id": i, "p0": "TOKEN"} for i in range(1000)], [{"id": 1000, "p0": "TOKEN"}],
        ]) as request:
            result = cloud_store.new_client_actions({CHAT})
        self.assertEqual(len(result), 1001)
        params = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(params["offset"], ["1000"])

    def test_compact_confirmation_preserves_full_payload_and_checks_revision(self):
        stored = {"text": "Original client details", "new_client_reminder_text": "Reminder",
                  "new_client_collected_details": {"business": "Test"}, "_first_delivery_at": NOW.isoformat()}
        with patch("cloud_store.request", side_effect=[
            [{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 1}],
        ]) as request:
            self.assertEqual(cloud_store.update_new_client_action(
                1, {"new_client_acknowledged_at": NOW.isoformat()}, status="cancelled"), {"id": 1})
        changes = request.call_args.args[1]
        self.assertEqual(changes["payload"]["text"], stored["text"])
        self.assertEqual(changes["payload"]["new_client_collected_details"], stored["new_client_collected_details"])
        params = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(params["updated_at"], ["eq." + NOW.isoformat()])
        self.assertEqual(params["select"], ["id"])
        self.assertEqual(params["payload->>new_client_acknowledged_at"], ["is.null"])

    def test_already_confirmed_assignment_is_not_updated(self):
        with patch("cloud_store.request", return_value=[]) as request:
            self.assertIsNone(cloud_store.update_freebie_action(1, {}, status="cancelled"))
        self.assertEqual(request.call_count, 1)

    def test_five_minute_slot_is_shared_and_cannot_send_a_message(self):
        with patch("cloud_store.request", side_effect=[[{"id": 1}], [], []]) as request:
            self.assertTrue(cloud_store.claim_automation_slot({CHAT}, NOW))
            self.assertFalse(cloud_store.claim_automation_slot({CHAT}, NOW + dt.timedelta(minutes=1)))
        first, second, update = request.call_args_list
        self.assertEqual(first.args[1]["dedupe_key"], second.args[1]["dedupe_key"])
        self.assertEqual(first.args[1]["status"], "cancelled")
        self.assertEqual(first.args[1]["payload"], second.args[1]["payload"])
        params = urllib.parse.parse_qs(update.args[0].split("?", 1)[1])
        self.assertEqual(params["payload->>automation_slot"], ["lt." + NOW.isoformat()])

    def test_next_slot_reuses_the_checkpoint_row(self):
        with patch("cloud_store.request", side_effect=[[], [{"id": 1}]]) as request:
            self.assertTrue(cloud_store.claim_automation_slot({CHAT}, NOW + dt.timedelta(minutes=5)))
        self.assertEqual(request.call_args.kwargs["method"], "PATCH")
        self.assertEqual(request.call_args.args[1]["payload"]["automation_slot"],
                         (NOW + dt.timedelta(minutes=5)).isoformat())

    def test_new_client_clock_advances_every_minute_independently_of_freebies(self):
        with patch("cloud_store.request", side_effect=[[{"id": 1}], [], [{"id": 1}], [{"id": 2}]]) as request:
            self.assertTrue(cloud_store.claim_automation_slot({CHAT}, NOW, workflow="new-client", interval_seconds=60))
            self.assertTrue(cloud_store.claim_automation_slot({CHAT}, NOW+dt.timedelta(minutes=1), workflow="new-client", interval_seconds=60))
            self.assertTrue(cloud_store.claim_automation_slot({CHAT}, NOW))
        first, _, update, freebie = request.call_args_list
        self.assertNotEqual(first.args[1]["dedupe_key"], freebie.args[1]["dedupe_key"])
        self.assertEqual(first.args[1]["status"], "cancelled")
        self.assertEqual(update.args[1]["payload"]["automation_slot"], (NOW+dt.timedelta(minutes=1)).isoformat())

    def test_duplicate_new_client_dispatch_in_same_minute_is_throttled(self):
        with patch("cloud_store.request", side_effect=[[], []]) as request:
            self.assertFalse(cloud_store.claim_automation_slot({CHAT}, NOW+dt.timedelta(seconds=59), workflow="new-client", interval_seconds=60))
        self.assertEqual(request.call_args.args[1]["payload"]["automation_slot"], NOW.isoformat())

    def test_new_clients_are_checked_while_freebie_scan_is_throttled(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "CRM_SUPABASE_SERVICE_ROLE_KEY": "configured"}), patch(
            "app.claim_automation_slot", side_effect=[True, False]
        ) as clock, patch("app.run_due_daily_automation", return_value=0), patch(
            "app.queue_new_client_assignments", return_value=1
        ) as clients, patch("app.queue_freebie_assignments") as freebies, patch(
            "app.deliver_due_actions", return_value=(1, 1, 0)
        ) as deliver:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 200)
        self.assertEqual(body["queued"], 1)
        clients.assert_called_once()
        freebies.assert_not_called()
        deliver.assert_called_once()
        self.assertEqual(clock.call_args_list[0].kwargs, {"workflow": "new-client", "interval_seconds": 60})

    def test_working_reply_lookup_excludes_prompts_and_paginates(self):
        with patch("cloud_store.request", side_effect=[
            [{"message_id": i, "text": "WORKING AABBCCDD"} for i in range(100)], [{"message_id": 100, "text": "TAKE AABBCCDD"}],
        ]) as request:
            result = cloud_store.new_client_reply_messages(CHAT, 4180, NOW, NOW,
                                                          author_ids={22, 11})
        self.assertEqual(len(result), 101)
        params = urllib.parse.parse_qs(request.call_args.args[0].split("?", 1)[1])
        self.assertEqual(params["author_id"], ["in.(11,22)"])
        self.assertEqual(params["offset"], ["100"])

    def test_completed_closeout_skips_report_and_history_downloads(self):
        with patch("daily_automation.existing_closeout_chats", return_value={CHAT}), patch(
            "daily_automation.build_group_report"
        ) as report, patch("daily_automation.daily_poll_counts") as counts:
            self.assertEqual(queue_daily_closeout(NOW.date(), NOW, {CHAT, DAILY_REPORTS_CHAT_ID}), 0)
        report.assert_not_called()
        counts.assert_not_called()

    def test_closeout_marker_is_not_written_after_partial_report_failure(self):
        with patch("daily_automation.existing_closeout_chats", return_value=set()), patch(
            "daily_automation.daily_poll_counts", return_value={}
        ), patch("daily_automation.build_group_report", return_value="Report"), patch(
            "daily_automation.enqueue_scheduled_action", side_effect=[True, RuntimeError("failed")]
        ), patch("daily_automation.record_automation_marker") as marker:
            with self.assertRaises(RuntimeError):
                queue_daily_closeout(NOW.date(), NOW, {CHAT, DAILY_REPORTS_CHAT_ID})
        marker.assert_not_called()

    def test_throttled_dispatch_still_delivers_due_reminders(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "CRM_SUPABASE_SERVICE_ROLE_KEY": "configured"}), patch(
            "app.claim_automation_slot", return_value=False
        ), patch("app.run_due_daily_automation", return_value=0), patch(
            "app.queue_new_client_assignments"
        ) as clients, patch("app.queue_freebie_assignments") as freebies, patch(
            "app.deliver_due_actions", return_value=(0, 0, 0)
        ) as deliver:
            status, _ = call_app("/api/cron/dispatch")
        self.assertEqual(status, 200)
        clients.assert_not_called()
        freebies.assert_not_called()
        deliver.assert_called_once_with({CHAT}, limit=25)


if __name__ == "__main__":
    unittest.main()
