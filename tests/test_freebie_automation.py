import datetime as dt
import unittest
from unittest.mock import patch

from freebie_automation import (
    confirm_freebie_reply, freebie_delivery_allowed, freebie_report_lines, freebie_status,
    queue_freebie_assignments,
)


CHAT = -1004461399292
NOW = dt.datetime(2026, 9, 19, 1, 0, tzinfo=dt.timezone.utc)  # 9 AM Manila


class FreebieAutomationTests(unittest.TestCase):
    def test_assigns_one_paid_contact_to_each_active_member(self):
        contacts = [
            {"id": "old", "name": "Old Client", "last_interaction_at": "2026-01-01"},
            {"id": "new", "name": "New Client", "last_interaction_at": "2026-08-01"},
        ]
        with patch("freebie_automation.freebie_actions", return_value=[]), patch(
            "freebie_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}
        ), patch("freebie_automation.daily_poll_active_users", return_value=[
            {"user_id": 11, "user_name": "A & B"}, {"user_id": 22, "user_name": "C"},
        ]), patch("freebie_automation.paid_contacts", return_value=contacts), patch(
            "freebie_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_freebie_assignments(NOW, {CHAT}), 2)
        first, second = [call.kwargs for call in enqueue.call_args_list]
        self.assertEqual(first["payload"]["freebie_contact_id"], "old")
        self.assertEqual(second["payload"]["freebie_contact_id"], "new")
        self.assertEqual(first["payload"]["message_thread_id"], 3003)
        self.assertEqual(first["repeat_interval_minutes"], 180)
        self.assertIn('tg://user?id=11', first["payload"]["text"])
        self.assertNotIn('tg://user?id=22', first["payload"]["text"])
        self.assertIn("A &amp; B", first["payload"]["text"])
        self.assertIn("Page: Onset Media Agency", first["payload"]["text"])
        self.assertIn("freebie task", first["payload"]["text"])
        self.assertIn("FREEBIE SENT", first["payload"]["text"])

    def test_freebie_uses_the_separate_freebie_topic(self):
        contacts = [{
            "id": "complete",
            "name": "Completed Client",
            "last_interaction_at": "2026-09-25T10:00:00+00:00",
            "stop_reason": "details_collected",
            "pipeline_stage": "qualified",
            "collected_details": {"business name": "Acme", "video length": "24 seconds"},
        }]
        with patch("freebie_automation.freebie_actions", return_value=[]), patch(
            "freebie_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}
        ), patch("freebie_automation.daily_poll_active_users", return_value=[
            {"user_id": 11, "user_name": "Alex"},
        ]), patch("freebie_automation.paid_contacts", return_value=contacts), patch(
            "freebie_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_freebie_assignments(NOW, {CHAT}), 1)
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["message_thread_id"], 3003)
        self.assertEqual(payload["freebie_thread_id"], 3003)
        self.assertNotIn("Details gathered by the chatbot", payload["text"])

    def test_recently_completed_contact_waits_fourteen_full_days(self):
        history = [{
            "id": 1, "chat_id": CHAT, "status": "cancelled",
            "payload": {
                "freebie_token": "AAAA1111", "freebie_assignee_id": 99,
                "freebie_contact_id": "recent",
                "freebie_completed_at": "2026-09-05T01:00:01+00:00",
            },
        }, {
            "id": 2, "chat_id": CHAT, "status": "cancelled",
            "payload": {
                "freebie_token": "BBBB2222", "freebie_assignee_id": 98,
                "freebie_contact_id": "ready",
                "freebie_completed_at": "2026-09-05T01:00:00+00:00",
            },
        }]
        contacts = [
            {"id": "recent", "name": "Recent Client", "last_interaction_at": "2026-01-01"},
            {"id": "ready", "name": "Ready Client", "last_interaction_at": "2026-08-01"},
        ]
        with patch("freebie_automation.freebie_actions", return_value=history), patch(
            "freebie_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}
        ), patch("freebie_automation.daily_poll_active_users", return_value=[
            {"user_id": 11, "user_name": "Alex"},
        ]), patch("freebie_automation.paid_contacts", return_value=contacts), patch(
            "freebie_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_freebie_assignments(NOW, {CHAT}), 1)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["freebie_contact_id"], "ready")

    def test_no_eligible_contact_sends_one_friendly_daily_notice(self):
        history = [{
            "id": 1, "chat_id": CHAT, "status": "cancelled",
            "payload": {
                "freebie_token": "AAAA1111", "freebie_assignee_id": 99,
                "freebie_contact_id": "recent",
                "freebie_completed_at": "2026-09-19T00:30:00+00:00",
            },
        }]
        contacts = [{"id": "recent", "name": "Recent Client", "last_interaction_at": "2026-01-01"}]
        with patch("freebie_automation.freebie_actions", return_value=history), patch(
            "freebie_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}
        ), patch("freebie_automation.daily_poll_active_users", return_value=[
            {"user_id": 11, "user_name": "Alex"},
        ]), patch("freebie_automation.paid_contacts", return_value=contacts), patch(
            "freebie_automation.enqueue_scheduled_action", side_effect=[True, False]
        ) as enqueue:
            self.assertEqual(queue_freebie_assignments(NOW, {CHAT}), 1)
            self.assertEqual(queue_freebie_assignments(NOW, {CHAT}), 0)
        notice = enqueue.call_args_list[0].kwargs
        self.assertNotIn("freebie_token", notice["payload"])
        self.assertIn("full 14-day break", notice["payload"]["text"])
        self.assertIn("No action is needed", notice["payload"]["text"])
        self.assertTrue(notice["payload"]["disable_notification"])
        self.assertEqual(
            notice["dedupe_key"],
            "freebie-unavailable:2026-09-19:-1004461399292:11",
        )

    def test_status_counts_only_contacts_available_after_cooldown(self):
        history = [{
            "id": 1, "chat_id": CHAT, "status": "cancelled",
            "payload": {
                "freebie_token": "AAAA1111", "freebie_assignee_id": 99,
                "freebie_contact_id": "recent",
                "freebie_completed_at": "2026-09-19T00:30:00+00:00",
            },
        }]
        contacts = [{"id": "recent", "name": "Recent Client", "last_interaction_at": "2026-01-01"}]
        with patch("freebie_automation.freebie_actions", return_value=history), patch(
            "freebie_automation.daily_poll_counts", return_value={}
        ), patch("freebie_automation.paid_contacts", return_value=contacts):
            status = freebie_status({CHAT}, NOW)
        self.assertEqual(status[0]["eligible_paid_contacts"], {"Onset Media Agency": 0})
        self.assertEqual(status[0]["cooling_down_contacts"], 1)

    def test_only_active_today_can_receive_reminder(self):
        action = {"id": 4, "chat_id": CHAT, "payload": {"freebie_token": "ABCDEF12", "freebie_assignee_id": 11}}
        with patch("freebie_automation.freebie_action_state", return_value={"status": "processing", "payload": {}}), patch(
            "freebie_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}
        ), patch("freebie_automation.daily_poll_active_users", return_value=[{"user_id": 22}]), patch(
            "freebie_automation.update_freebie_action", return_value=action
        ) as cancel:
            self.assertFalse(freebie_delivery_allowed(action, NOW))
        self.assertEqual(cancel.call_args.kwargs["status"], "cancelled")
        self.assertEqual(
            cancel.call_args.args[1]["freebie_cancelled_reason"],
            "assignee_inactive_before_first_delivery",
        )

    def test_completion_requires_matching_member_thread_and_token(self):
        row = {"id": 4, "chat_id": CHAT, "payload": {
            "freebie_token": "ABCDEF12", "freebie_assignee_id": 11,
        }}
        update = {"message": {"chat": {"id": CHAT}, "message_thread_id": 3003,
                              "from": {"id": 11}, "text": "FREEBIE SENT ABCDEF12",
                              "date": 1789780000, "message_id": 90}}
        with patch("freebie_automation.freebie_actions", return_value=[row]), patch(
            "freebie_automation.update_freebie_action", return_value=row
        ) as update_action, patch("freebie_automation.enqueue_scheduled_action", return_value=True) as enqueue, patch(
            "freebie_automation.queue_freebie_assignments"
        ) as queue:
            self.assertTrue(confirm_freebie_reply(update, {CHAT}))
            self.assertEqual(update_action.call_args.kwargs["status"], "cancelled")
            self.assertEqual(update_action.call_args.args[1]["freebie_completion_message_id"], 90)
            self.assertIn("marked as sent", enqueue.call_args.kwargs["payload"]["text"])
            self.assertEqual(enqueue.call_args.kwargs["dedupe_key"], "freebie-confirmation:4")
            queue.assert_called_once()
            update["message"]["from"]["id"] = 22
            self.assertFalse(confirm_freebie_reply(update, {CHAT}))

    def test_freebie_confirmation_is_rejected_in_new_client_topic(self):
        row = {"id": 4, "chat_id": CHAT, "payload": {
            "freebie_token": "ABCDEF12", "freebie_assignee_id": 11,
            "freebie_thread_id": 3003,
        }}
        update = {"message": {"chat": {"id": CHAT}, "message_thread_id": 4180,
                              "from": {"id": 11}, "text": "FREEBIE SENT ABCDEF12",
                              "date": 1789780000, "message_id": 90}}
        with patch("freebie_automation.freebie_actions", return_value=[row]), patch(
            "freebie_automation.update_freebie_action", return_value=row
        ), patch("freebie_automation.enqueue_scheduled_action", return_value=True), patch(
            "freebie_automation.queue_freebie_assignments"
        ):
            self.assertFalse(confirm_freebie_reply(update, {CHAT}))

    def test_reports_confirmations_in_manila_day(self):
        rows = [
            {"payload": {"freebie_completed_at": "2026-09-18T17:10:00+00:00", "freebie_page": "Onset Media Agency", "freebie_assignee_id": 11, "freebie_assignee_name": "Alex"}},
            {"payload": {"freebie_completed_at": "2026-09-18T18:10:00+00:00", "freebie_page": "Onset Media Agency", "freebie_assignee_id": 11, "freebie_assignee_name": "Alex"}},
        ]
        with patch("freebie_automation.freebie_actions", return_value=rows):
            lines = freebie_report_lines(CHAT, dt.date(2026, 9, 19))
        self.assertIn("Team total: 2", lines)
        self.assertIn("• Onset Media Agency: 2", lines)
        self.assertIn("• Alex: 2", lines)


if __name__ == "__main__":
    unittest.main()
