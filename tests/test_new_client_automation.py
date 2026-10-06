import datetime as dt
import unittest
from unittest.mock import patch

from new_client_automation import (
    confirm_new_client_reply,
    new_client_delivery_allowed,
    new_client_report_lines,
    new_client_status,
    queue_new_client_assignments,
)


CHAT = -1004461399292
NOW = dt.datetime(2026, 9, 19, 1, 0, tzinfo=dt.timezone.utc)  # 9 AM Manila


def contact(
    contact_id: str, date: str, *, psid: str | None = None, name: str | None = None,
) -> dict:
    return {
        "id": contact_id,
        "page_id": "page-1",
        "psid": psid or f"psid-{contact_id}",
        "name": name or f"Client {contact_id}",
        "last_interaction_at": date,
        "pipeline_stage": "qualified",
        "stop_reason": "details_collected",
        "collected_details": {"business name": f"Business {contact_id}", "video length": "30 seconds"},
        "missing_details": [],
    }


def assignment(
    action_id: int, user_id: int, contact_id: str, round_number: int, *,
    acknowledged=False, status="cancelled", assigned_at: dt.datetime | None = None,
    telegram_message_id: int | None = None, psid: str | None = None,
    contact_name: str | None = None, cancelled_reason: str | None = None,
) -> dict:
    payload = {
        "new_client_token": f"TOKEN{action_id:03d}"[-8:],
        "new_client_assignee_id": user_id,
        "new_client_assignee_name": f"User {user_id}",
        "new_client_contact_id": contact_id,
        "new_client_contact_page_id": "page-1",
        "new_client_contact_name": contact_name or f"Client {contact_id}",
        "new_client_page": "Onset Media Agency",
        "new_client_thread_id": 4180,
        "new_client_work_date": "2026-09-19",
        "new_client_round": round_number,
    }
    if assigned_at:
        payload["new_client_assigned_at"] = assigned_at.isoformat()
    if psid:
        payload["new_client_contact_psid"] = psid
        payload["new_client_contact_identity"] = f"psid:page-1:{psid}"
    if cancelled_reason:
        payload["new_client_cancelled_reason"] = cancelled_reason
    if acknowledged:
        payload["new_client_acknowledged_at"] = "2026-09-19T01:10:00+00:00"
    return {
        "id": action_id, "chat_id": CHAT, "status": status,
        "sent_at": assigned_at.isoformat() if assigned_at else None,
        "telegram_message_id": telegram_message_id, "payload": payload,
    }


class NewClientAutomationTests(unittest.TestCase):
    def setUp(self):
        self.reply_messages_patcher = patch(
            "new_client_automation.new_client_reply_messages", return_value=[]
        )
        self.reply_messages = self.reply_messages_patcher.start()
        self.addCleanup(self.reply_messages_patcher.stop)
        self.identity_map_patcher = patch(
            "new_client_automation.contact_identity_map", return_value={}
        )
        self.identity_map = self.identity_map_patcher.start()
        self.addCleanup(self.identity_map_patcher.stop)

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

    def test_duplicate_crm_rows_with_same_psid_are_queued_only_once(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        contacts = [
            contact("old-row", "2026-01-01", psid="same-person", name="Original Name"),
            contact("new-row", "2026-01-02", psid="same-person", name="Updated Name"),
        ]
        p1, p2, p3, p4 = self.active_patches(members, [], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)
        enqueue.assert_called_once()

    def test_crm_row_replacement_keeps_the_same_database_dedupe_key(self):
        members = [{"user_id": 11, "user_name": "Alex"}]
        dedupe_keys = []
        for row_id, name in (("old-row", "Original Name"), ("new-row", "Updated Name")):
            p1, p2, p3, p4 = self.active_patches(
                members, [], [contact(row_id, "2026-01-01", psid="same-person", name=name)],
            )
            with p1, p2, p3, p4, patch(
                "new_client_automation.enqueue_scheduled_action", return_value=True
            ) as enqueue:
                self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)
            dedupe_keys.append(enqueue.call_args.kwargs["dedupe_key"])
        self.assertEqual(dedupe_keys[0], dedupe_keys[1])
        self.assertIn("psid:page-1:same-person", dedupe_keys[0])

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

    def test_working_token_accepts_extra_text_and_missing_thread_field(self):
        row = assignment(5, 11, "client", 1, status="pending")
        row["payload"]["new_client_token"] = "ABCDEF12"
        update = {"message": {
            "chat": {"id": CHAT}, "from": {"id": 11},
            "text": "Working ABCDEF12, starting now!", "date": 1789780000, "message_id": 91,
        }}
        with patch("new_client_automation.new_client_actions", return_value=[row]), patch(
            "new_client_automation.update_new_client_action", return_value=row
        ), patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ), patch("new_client_automation.queue_new_client_assignments"):
            self.assertTrue(confirm_new_client_reply(update, {CHAT}))

    def test_direct_working_reply_does_not_require_token(self):
        row = assignment(6, 11, "client", 1, status="pending", telegram_message_id=500)
        update = {"message": {
            "chat": {"id": CHAT}, "message_thread_id": 4180,
            "reply_to_message": {"message_id": 500},
            "from": {"id": 11}, "text": "working", "date": 1789780000, "message_id": 92,
        }}
        with patch("new_client_automation.new_client_actions", return_value=[row]), patch(
            "new_client_automation.update_new_client_action", return_value=row
        ), patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ), patch("new_client_automation.queue_new_client_assignments"):
            self.assertTrue(confirm_new_client_reply(update, {CHAT}))

    def test_archived_working_reply_prevents_timeout_reassignment(self):
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        stale = assignment(
            7, 11, "client", 1, status="pending",
            assigned_at=NOW - dt.timedelta(hours=2),
        )
        stale["payload"]["new_client_token"] = "ABCDEF12"
        self.reply_messages.return_value = [{
            "message_id": 93,
            "sent_utc": (NOW - dt.timedelta(minutes=90)).isoformat(),
            "author_id": 11,
            "text": "WORKING ABCDEF12.",
            "thread_id": 4180,
            "reply_to_message_id": None,
        }]
        contacts = [contact("client", "2026-01-01")]
        p1, p2, p3, p4 = self.active_patches(members, [stale], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action", return_value=stale
        ) as update_action, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)

        self.assertIn("new_client_acknowledged_at", update_action.call_args.args[1])
        self.assertEqual(enqueue.call_count, 1)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].startswith("new-client-ack:"))

    def test_late_working_reply_acknowledges_timed_out_assignment(self):
        row = assignment(
            8, 11, "client", 1, status="cancelled",
            assigned_at=NOW - dt.timedelta(hours=2),
            cancelled_reason="working_not_confirmed_within_one_hour",
        )
        row["payload"]["new_client_token"] = "ABCDEF12"
        update = {"message": {
            "chat": {"id": CHAT}, "message_thread_id": 4180,
            "from": {"id": 11}, "text": "WORKING ABCDEF12",
            "date": int(NOW.timestamp()), "message_id": 94,
        }}
        with patch("new_client_automation.new_client_actions", return_value=[row]), patch(
            "new_client_automation.update_new_client_action", return_value=row
        ) as update_action, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ), patch("new_client_automation.queue_new_client_assignments"):
            self.assertTrue(confirm_new_client_reply(update, {CHAT}))

        self.assertTrue(update_action.call_args.kwargs["include_cancelled"])
        self.assertEqual(update_action.call_args.args[1]["new_client_ack_message_id"], 94)

    def test_contact_detail_update_does_not_reassign_acknowledged_client(self):
        members = [{"user_id": 11, "user_name": "Alex"}]
        history = [assignment(
            9, 11, "old-row", 1, acknowledged=True,
            contact_name="Original Name",
        )]
        self.identity_map.return_value = {"old-row": ("page-1", "same-person")}
        contacts = [contact(
            "replacement-row", "2026-09-18", psid="same-person", name="Updated Name",
        )]
        p1, p2, p3, p4 = self.active_patches(members, history, contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.enqueue_scheduled_action"
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)
        enqueue.assert_not_called()

    def test_late_reply_cancels_duplicate_reassignment_for_same_contact(self):
        original = assignment(
            10, 11, "old-row", 1, status="cancelled",
            assigned_at=NOW - dt.timedelta(hours=2), psid="same-person",
            contact_name="Original Name",
            cancelled_reason="working_not_confirmed_within_one_hour",
        )
        original["payload"]["new_client_token"] = "ABCDEF12"
        duplicate = assignment(
            11, 22, "replacement-row", 2, status="pending",
            assigned_at=NOW - dt.timedelta(minutes=45), psid="same-person",
            contact_name="Updated Name",
        )
        self.reply_messages.return_value = [{
            "message_id": 95,
            "sent_utc": (NOW - dt.timedelta(minutes=30)).isoformat(),
            "author_id": 11,
            "text": "WORKING ABCDEF12",
            "thread_id": 4180,
            "reply_to_message_id": None,
        }]
        members = [{"user_id": 11, "user_name": "Alex"}, {"user_id": 22, "user_name": "Bea"}]
        contacts = [contact(
            "replacement-row", "2026-09-18", psid="same-person", name="Updated Name",
        )]
        p1, p2, p3, p4 = self.active_patches(members, [original, duplicate], contacts)
        with p1, p2, p3, p4, patch(
            "new_client_automation.update_new_client_action", side_effect=lambda *args, **kwargs: args[1]
        ) as update_action, patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 0)

        self.assertEqual(update_action.call_count, 2)
        self.assertEqual(update_action.call_args_list[0].args[0], 10)
        self.assertTrue(update_action.call_args_list[0].kwargs["include_cancelled"])
        self.assertEqual(update_action.call_args_list[1].args[0], 11)
        self.assertEqual(
            update_action.call_args_list[1].args[1]["new_client_cancelled_reason"],
            "contact_already_acknowledged",
        )
        self.assertEqual(enqueue.call_count, 1)
        self.assertTrue(enqueue.call_args.kwargs["dedupe_key"].startswith("new-client-ack:"))

    def test_assignment_expires_after_its_philippine_day(self):
        action = assignment(4, 11, "client", 1, status="processing")
        with patch("new_client_automation.update_new_client_action", return_value=action) as update:
            self.assertFalse(new_client_delivery_allowed(action, NOW + dt.timedelta(days=1)))
        self.assertEqual(update.call_args.args[1]["new_client_cancelled_reason"], "assignment_day_ended")

    def test_acknowledged_assignment_cannot_send_an_hourly_reminder(self):
        action = assignment(12, 11, "client", 1, status="processing")
        action["sent_at"] = "2026-09-19T00:00:00+00:00"
        live = {
            "status": "cancelled",
            "payload": {**action["payload"], "new_client_acknowledged_at": NOW.isoformat()},
        }
        with patch("new_client_automation.new_client_action_state", return_value=live):
            self.assertFalse(new_client_delivery_allowed(action, NOW))

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


VEO_CHAT = -1003647732254


def veo_assignment(*args, **kwargs):
    row = assignment(*args, **kwargs)
    row["chat_id"] = VEO_CHAT
    row["payload"].update(new_client_page="Azshinari", new_client_thread_id=27622)
    if row["payload"].get("new_client_acknowledged_at"):
        row["payload"]["new_client_acknowledged_at"] = (NOW - dt.timedelta(seconds=1)).isoformat()
    return row


class VeoRotationTests(unittest.TestCase):
    def setUp(self):
        self.members = [
            {"user_id": 11, "user_name": "Alex", "updated_at": (NOW - dt.timedelta(hours=2)).isoformat()},
            {"user_id": 22, "user_name": "Bea", "updated_at": (NOW - dt.timedelta(hours=2)).isoformat()},
        ]
        self.history = []
        self.contacts = []
        for target, value in (
            ("new_client_actions", self.history), ("_active_members", self.members),
            ("_page_contacts", [("Azshinari", self.contacts)]),
            ("contact_identity_map", {}), ("new_client_reply_messages", []),
        ):
            patcher = patch(f"new_client_automation.{target}", return_value=value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.update_patcher = patch("new_client_automation.update_new_client_action", return_value=True)
        self.update = self.update_patcher.start(); self.addCleanup(self.update_patcher.stop)
        self.enqueue_patcher = patch("new_client_automation.enqueue_scheduled_action", return_value=True)
        self.enqueue = self.enqueue_patcher.start(); self.addCleanup(self.enqueue_patcher.stop)

    def queue(self):
        return queue_new_client_assignments(NOW, {VEO_CHAT})

    def test_timeout_is_exactly_30_minutes_from_first_delivery_and_pauses_member(self):
        stale = veo_assignment(1, 11, "client", 1, status="pending", assigned_at=NOW-dt.timedelta(minutes=30))
        self.history.append(stale); self.contacts.extend([contact("client", "2026-01-01"), contact("fresh", "2026-01-02")])
        self.assertEqual(self.queue(), 1)
        self.assertEqual(stale["payload"]["new_client_cancelled_reason"], "working_not_confirmed_within_30_minutes")
        payload = self.enqueue.call_args.kwargs["payload"]
        self.assertEqual((payload["new_client_assignee_id"], payload["new_client_contact_id"]), (22, "client"))
        self.assertEqual(self.enqueue.call_args.kwargs["repeat_interval_minutes"], 30)
        self.assertIn("within 30 minutes", payload["text"])

    def test_no_timeout_one_second_before_deadline(self):
        current = veo_assignment(1, 11, "client", 1, status="pending", assigned_at=NOW-dt.timedelta(minutes=29, seconds=59))
        self.history.append(current); self.contacts.append(contact("client", "2026-01-01"))
        self.assertEqual(self.queue(), 0)
        self.update.assert_not_called(); self.enqueue.assert_not_called()

    def test_queue_age_and_repeated_delivery_do_not_change_first_delivery_deadline(self):
        current = veo_assignment(1, 11, "client", 1, status="pending", assigned_at=NOW-dt.timedelta(hours=2))
        current["payload"]["_first_delivery_at"] = (NOW-dt.timedelta(minutes=30)).isoformat()
        current["sent_at"] = (NOW-dt.timedelta(minutes=1)).isoformat()
        self.history.append(current); self.contacts.append(contact("client", "2026-01-01"))
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)

    def test_recent_first_delivery_does_not_timeout_old_queue_entry(self):
        current = veo_assignment(1, 11, "client", 1, status="pending", assigned_at=NOW-dt.timedelta(hours=2))
        current["payload"]["_first_delivery_at"] = (NOW-dt.timedelta(minutes=10)).isoformat()
        self.history.append(current); self.contacts.append(contact("client", "2026-01-01"))
        self.assertEqual(self.queue(), 0); self.update.assert_not_called()

    def test_undelivered_client_does_not_start_timeout(self):
        current = veo_assignment(1, 11, "client", 1, status="pending", assigned_at=NOW-dt.timedelta(hours=2))
        current["sent_at"] = None
        self.history.append(current); self.contacts.append(contact("client", "2026-01-01"))
        self.assertEqual(self.queue(), 0); self.update.assert_not_called()

    def test_ready_member_keeps_receiving_clients_while_peer_waits_for_ack(self):
        self.history.extend([
            veo_assignment(1, 11, "done1", 1, acknowledged=True),
            veo_assignment(2, 11, "done2", 2, acknowledged=True),
            veo_assignment(3, 22, "waiting", 1, status="pending", assigned_at=NOW-dt.timedelta(minutes=5)),
        ])
        self.contacts.append(contact("fresh", "2026-01-01"))
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 11)

    def test_fewer_assignments_take_priority_over_poll_name_order(self):
        self.members.reverse()
        self.history.extend([veo_assignment(1, 22, "done1", 1, acknowledged=True), veo_assignment(2, 22, "done2", 2, acknowledged=True)])
        self.contacts.append(contact("fresh", "2026-01-01"))
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 11)

    def test_equal_counts_favor_member_who_waited_longest(self):
        self.history.extend([
            veo_assignment(1, 11, "done1", 1, acknowledged=True, assigned_at=NOW-dt.timedelta(minutes=5)),
            veo_assignment(2, 22, "done2", 1, acknowledged=True, assigned_at=NOW-dt.timedelta(minutes=20)),
        ])
        self.contacts.append(contact("fresh", "2026-01-01"))
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)

    def test_failed_offer_does_not_count_against_member_after_renewed_active_vote(self):
        timed_out = veo_assignment(1, 11, "missed", 1, cancelled_reason="working_not_confirmed_within_30_minutes")
        timed_out["payload"]["new_client_cancelled_at"] = (NOW-dt.timedelta(minutes=5)).isoformat()
        self.members[0]["updated_at"] = (NOW-dt.timedelta(minutes=1)).isoformat()
        self.history.extend([timed_out, veo_assignment(2, 22, "done", 1, acknowledged=True)])
        self.contacts.append(contact("fresh", "2026-01-01"))
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 11)

    def test_timeout_contact_is_rescued_before_older_fresh_contact(self):
        timed_out = veo_assignment(1, 11, "missed", 1, cancelled_reason="working_not_confirmed_within_30_minutes")
        timed_out["payload"]["new_client_cancelled_at"] = (NOW-dt.timedelta(minutes=5)).isoformat()
        self.history.append(timed_out)
        self.contacts.extend([contact("older-fresh", "2026-01-01"), contact("missed", "2026-09-01")])
        self.assertEqual(self.queue(), 1)
        self.assertEqual(self.enqueue.call_args.kwargs["payload"]["new_client_contact_id"], "missed")

    def test_late_working_reply_after_30_minute_timeout_is_still_reconciled(self):
        original = veo_assignment(1, 11, "missed", 1, assigned_at=NOW-dt.timedelta(hours=1), cancelled_reason="working_not_confirmed_within_30_minutes")
        original["payload"].update(new_client_token="ABCDEF12", new_client_cancelled_at=(NOW-dt.timedelta(minutes=30)).isoformat())
        self.history.append(original)
        with patch("new_client_automation.new_client_reply_messages", return_value=[{
            "message_id": 99, "sent_utc": (NOW-dt.timedelta(minutes=2)).isoformat(),
            "author_id": 11, "text": "WORKING ABCDEF12", "thread_id": 27622,
        }]):
            self.assertEqual(self.queue(), 0)
        self.assertIn("new_client_acknowledged_at", original["payload"])
        self.assertTrue(self.update.call_args.kwargs["include_cancelled"])

    def test_status_exposes_paused_member_and_ready_count(self):
        timed_out = veo_assignment(1, 11, "missed", 1, cancelled_reason="working_not_confirmed_within_30_minutes")
        timed_out["payload"]["new_client_cancelled_at"] = (NOW-dt.timedelta(minutes=5)).isoformat()
        self.history.append(timed_out)
        status = new_client_status({VEO_CHAT}, NOW)[0]
        self.assertEqual(status["reply_deadline_minutes"], 30)
        self.assertEqual(status["ready_today"], 1)
        self.assertEqual(status["paused_members"], [{"user_id": 11, "name": "Alex"}])
        self.update.assert_not_called(); self.enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
