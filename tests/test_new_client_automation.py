import datetime as dt
import unittest
from unittest.mock import patch

from new_client_automation import (
    confirm_new_client_reply,
    new_client_delivery_allowed,
    new_client_report_lines,
    queue_new_client_assignments,
)


CHAT = -1004461399292
NOW = dt.datetime(2026, 9, 19, 1, 0, tzinfo=dt.timezone.utc)  # 9 AM Manila


def contact(contact_id: str, date: str) -> dict:
    return {
        "id": contact_id,
        "name": f"Client {contact_id}",
        "last_interaction_at": date,
        "pipeline_stage": "qualified",
        "stop_reason": "details_collected",
        "collected_details": {"business name": f"Business {contact_id}", "video length": "30 seconds"},
        "missing_details": [],
    }


def assignment(
    action_id: int, user_id: int, contact_id: str, round_number: int, *,
    acknowledged=False, status="cancelled", assigned_at: dt.datetime | None = None,
) -> dict:
    payload = {
        "new_client_token": f"TOKEN{action_id:03d}"[-8:],
        "new_client_assignee_id": user_id,
        "new_client_assignee_name": f"User {user_id}",
        "new_client_contact_id": contact_id,
        "new_client_page": "Onset Media Agency",
        "new_client_thread_id": 4180,
        "new_client_work_date": "2026-09-19",
        "new_client_round": round_number,
    }
    if assigned_at:
        payload["new_client_assigned_at"] = assigned_at.isoformat()
    if acknowledged:
        payload["new_client_acknowledged_at"] = "2026-09-19T01:10:00+00:00"
    return {"id": action_id, "chat_id": CHAT, "status": status, "payload": payload}


class NewClientAutomationTests(unittest.TestCase):
    def active_patches(self, members, history, contacts):
        return (
            patch("new_client_automation.new_client_actions", return_value=history),
            patch("new_client_automation.daily_poll_counts", return_value={CHAT: {"poll_id": "poll"}}),
            patch("new_client_automation.daily_poll_active_users", return_value=members),
            patch("new_client_automation.completed_detail_contacts", return_value=contacts),
        )

    def test_first_round_gives_each_active_member_one_complete_client(self):
        members = [{"user_id": 11, "user_name": "A & B"}, {"user_id": 22, "user_name": "C"}]
        contacts = [contact("old", "2026-01-01"), contact("new", "2026-08-01"), contact("later", "2026-09-01")]
        p1, p2, p3, p4 = self.active_patches(members, [], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 2)

        first, second = [call.kwargs for call in enqueue.call_args_list]
        self.assertEqual(first["payload"]["new_client_assignee_id"], 11)
        self.assertEqual(second["payload"]["new_client_assignee_id"], 22)
        self.assertEqual(first["payload"]["new_client_contact_id"], "old")
        self.assertEqual(second["payload"]["new_client_contact_id"], "new")
        self.assertEqual(first["payload"]["new_client_round"], 1)
        self.assertEqual(first["payload"]["message_thread_id"], 4180)
        self.assertIn("Complete details from Supabase", first["payload"]["text"])
        self.assertIn("Business old", first["payload"]["text"])
        self.assertIn("WORKING", first["payload"]["text"])
        self.assertNotIn("freebie", first["payload"]["text"].casefold())

    def test_round_robin_catches_up_member_before_leader_gets_third(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        history = [
            assignment(1, 11, "a1", 1, acknowledged=True),
            assignment(2, 22, "b1", 1, acknowledged=True),
            assignment(3, 11, "a2", 2, acknowledged=True),
        ]
        contacts = [
            contact("a1", "2026-01-01"), contact("b1", "2026-01-02"),
            contact("a2", "2026-01-03"), contact("next", "2026-01-04"),
        ]
        p1, p2, p3, p4 = self.active_patches(members, history, contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)

        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["new_client_assignee_id"], 22)
        self.assertEqual(payload["new_client_round"], 2)
        self.assertEqual(payload["new_client_contact_id"], "next")

    def test_member_must_acknowledge_before_receiving_another(self):
        members = [{"user_id": 11, "user_name": "Alex"}]
        history = [assignment(1, 11, "open", 1, acknowledged=False, status="pending")]
        contacts = [contact("open", "2026-01-01"), contact("next", "2026-01-02")]
        p1, p2, p3, p4 = self.active_patches(members, history, contacts)
        with p1, p2, p3, p4, patch("new_client_automation.enqueue_scheduled_action") as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)
        enqueue.assert_not_called()

    def test_unconfirmed_contact_is_reassigned_after_one_hour(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        stale = assignment(
            1, 11, "client", 1, status="pending",
            assigned_at=NOW - dt.timedelta(hours=1, seconds=1),
        )
        contacts = [contact("client", "2026-01-01")]
        p1, p2, p3, p4 = self.active_patches(members, [stale], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action", return_value=stale
        ) as update, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)

        cancelled = update.call_args.args[1]
        self.assertEqual(cancelled["new_client_cancelled_reason"], "working_not_confirmed_within_one_hour")
        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_contact_id"], "client")
        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_assignment_attempt"], 2)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].endswith(":attempt-2"))
        self.assertEqual(enqueue.call_args.kwargs["repeat_interval_minutes"], 60)

    def test_assignment_is_not_reassigned_before_one_full_hour(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        current = assignment(
            1, 11, "client", 1, status="pending",
            assigned_at=NOW - dt.timedelta(minutes=59),
        )
        contacts = [contact("client", "2026-01-01")]
        p1, p2, p3, p4 = self.active_patches(members, [current], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action"
        ) as update, patch("new_client_automation.enqueue_scheduled_action") as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)
        update.assert_not_called()
        enqueue.assert_not_called()

    def test_timed_out_contact_is_not_returned_to_same_member(self):
        members = [{"user_id": 11, "user_name": "Alex"}]
        stale = assignment(
            1, 11, "client", 1, status="pending",
            assigned_at=NOW - dt.timedelta(hours=2),
        )
        contacts = [contact("client", "2026-01-01")]
        p1, p2, p3, p4 = self.active_patches(members, [stale], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action", return_value=stale
        ), patch("new_client_automation.enqueue_scheduled_action") as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)
        enqueue.assert_not_called()

    def test_reassignment_overrides_normal_round_minimum(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        stale = assignment(
            1, 11, "client", 1, status="pending",
            assigned_at=NOW - dt.timedelta(hours=2),
        )
        history = [
            stale,
            assignment(2, 22, "done-1", 1, acknowledged=True),
            assignment(3, 22, "done-2", 2, acknowledged=True),
        ]
        contacts = [
            contact("client", "2026-01-01"),
            contact("done-1", "2026-01-02"),
            contact("done-2", "2026-01-03"),
        ]
        p1, p2, p3, p4 = self.active_patches(members, history, contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action", return_value=stale
        ), patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)

        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_contact_id"], "client")

    def test_working_reply_acknowledges_exact_member_topic_and_token(self):
        row = assignment(4, 11, "client", 1, status="pending")
        row["payload"]["new_client_token"] = "ABCDEF12"
        update = {"message": {
            "chat": {"id": CHAT}, "message_thread_id": 4180,
            "from": {"id": 11}, "text": "WORKING ABCDEF12",
            "date": 1789780000, "message_id": 90,
        }}
        with patch("new_client_automation.new_client_actions", return_value=[row]), patch(
            "new_client_automation.update_new_client_action", return_value=row
        ) as update_action, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue, patch("new_client_automation.queue_new_client_assignments") as queue:
            self.assertTrue(confirm_new_client_reply(update, {CHAT}))

        self.assertEqual(update_action.call_args.kwargs["status"], "cancelled")
        self.assertEqual(update_action.call_args.args[1]["new_client_ack_message_id"], 90)
        self.assertIn("working on this client", enqueue.call_args.kwargs["payload"]["text"])
        queue.assert_called_once()

    def test_assignment_expires_after_its_philippine_day(self):
        action = assignment(4, 11, "client", 1, status="processing")
        with patch("new_client_automation.update_new_client_action", return_value=action) as update:
            self.assertFalse(new_client_delivery_allowed(action, NOW + dt.timedelta(days=1)))
        self.assertEqual(update.call_args.args[1]["new_client_cancelled_reason"], "assignment_day_ended")

    def test_daily_report_counts_working_confirmations(self):
        rows = [
            assignment(1, 11, "a", 1, acknowledged=True),
            assignment(2, 11, "b", 2, acknowledged=True),
            assignment(3, 22, "c", 1, acknowledged=False, status="pending"),
        ]
        with patch("new_client_automation.new_client_actions", return_value=rows):
            lines = new_client_report_lines(CHAT, dt.date(2026, 9, 19))
        self.assertIn("Team total: 2", lines)
        self.assertIn("• User 11: 2", lines)


if __name__ == "__main__":
    unittest.main()
