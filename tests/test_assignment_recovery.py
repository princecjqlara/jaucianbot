import copy
import datetime as dt
import unittest
from unittest.mock import patch

import app
import client_volunteers as volunteers
import new_client_automation as clients
from test_hourly_assignments import CHAT, MEMBERS, NOW, offer


class AssignmentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.history = []
        self.members = copy.deepcopy(MEMBERS[:2])
        self.contact = {"id": "client", "page_id": "page", "psid": "person",
                        "name": "Client", "collected_details": {"purpose": "ad"}}
        for patcher in (
            patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "true",
                                       "HOURLY_AVAILABILITY_START_DATE": "2026-10-09"}),
            patch.object(clients, "new_client_actions", return_value=self.history),
            patch.object(clients, "_active_members", side_effect=lambda *args: self.members),
            patch.object(clients, "new_client_reply_messages", return_value=[]),
            patch.object(clients, "contact_identity_map", return_value={}),
            patch.object(clients, "_page_contacts", return_value=[("Onset Media Agency", [self.contact])]),
            patch.object(volunteers, "update_new_client_action", return_value=True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.update = patch.object(clients, "update_new_client_action", return_value=True)
        self.update_mock = self.update.start()
        self.addCleanup(self.update.stop)
        self.enqueue = patch.object(clients, "enqueue_scheduled_action", return_value=True)
        self.enqueue_mock = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

    def unsent_offer(self, status="pending"):
        row = offer()
        row.update(status=status, sent_at=None, telegram_message_id=None)
        row["payload"].pop("_first_delivery_at")
        self.history.append(row)
        return row

    def test_yesterdays_unanswered_client_can_return_to_only_available_editor(self):
        self.members[:] = self.members[:1]
        row = self.unsent_offer()
        row["payload"]["new_client_work_date"] = "2026-10-08"
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 1)
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], "assignment_day_ended")
        payload = self.enqueue_mock.call_args.kwargs["payload"]
        self.assertEqual(payload["new_client_assignee_id"], 11)
        self.assertEqual(payload["new_client_assignment_attempt"], 2)

    def test_undelivered_offer_releases_when_assignee_leaves_selected_hour(self):
        self.members[0]["available_hours"] = [8]
        row = self.unsent_offer()
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 1)
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], "assignee_inactive_before_first_delivery")
        self.assertEqual(self.enqueue_mock.call_args.kwargs["payload"]["new_client_assignee_id"], 22)

    def test_editor_can_receive_offer_that_was_cancelled_before_delivery(self):
        self.members[:] = self.members[:1]
        row = self.unsent_offer("cancelled")
        row["payload"]["new_client_cancelled_reason"] = "assignee_inactive_before_first_delivery"
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 1)
        self.assertEqual(self.enqueue_mock.call_args.kwargs["payload"]["new_client_assignee_id"], 11)

    def test_exhausted_delivery_failure_releases_to_another_editor(self):
        row = self.unsent_offer("failed")
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 1)
        self.assertEqual(row["payload"]["new_client_cancelled_reason"], "assignment_delivery_failed")
        self.assertEqual(self.enqueue_mock.call_args.kwargs["payload"]["new_client_assignee_id"], 22)

    def test_failed_offer_waits_for_next_day_if_only_editor_cannot_receive_it(self):
        self.members[:] = self.members[:1]
        row = self.unsent_offer("failed")
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 0)
        self.assertEqual(row["status"], "cancelled")
        self.enqueue_mock.assert_not_called()
        self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(days=1), {CHAT}), 1)

    def test_still_available_editor_keeps_unsent_offer_without_response_timeout(self):
        row = self.unsent_offer()
        row["payload"]["new_client_assigned_at"] = (NOW - dt.timedelta(hours=2)).isoformat()
        self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 0)
        self.update_mock.assert_not_called()

    def test_status_distinguishes_undelivered_offer_from_working_reply(self):
        self.unsent_offer()
        status = clients.new_client_status({CHAT}, NOW)[0]
        self.assertEqual(status["members"][0]["state"], "awaiting_delivery")

    def test_status_identifies_failed_delivery(self):
        self.unsent_offer("failed")
        status = clients.new_client_status({CHAT}, NOW)[0]
        self.assertEqual(status["members"][0]["state"], "delivery_failed")


class DeliveryBacklogTests(unittest.TestCase):
    def test_deferred_reminders_do_not_consume_whole_delivery_budget(self):
        reminders = [{"id": i, "chat_id": CHAT, "payload": {"freebie_token": "TOKEN"}}
                     for i in range(25)]
        fresh = {"id": 30, "chat_id": CHAT, "action_type": "message", "payload": {"text": "New offer"}}
        with patch.object(app, "claim_scheduled_actions", side_effect=[reminders, [fresh]]) as claim, \
                patch.object(app, "freebie_delivery_allowed", return_value=False), \
                patch.object(app, "defer_scheduled_action", return_value=True), \
                patch.object(app, "send_scheduled_action", return_value={"message_id": 900}) as send, \
                patch.object(app, "finish_scheduled_action", return_value=True), \
                patch.object(app, "save_update"):
            self.assertEqual(app.deliver_due_actions({CHAT}, limit=25), (26, 1, 0))
        self.assertEqual(claim.call_count, 2)
        send.assert_called_once_with(fresh)

    def test_suppression_scan_has_finite_budget(self):
        batches = [[{"id": start + i, "chat_id": CHAT, "payload": {"freebie_token": "TOKEN"}}
                    for i in range(25)] for start in range(0, 100, 25)]
        with patch.object(app, "claim_scheduled_actions", side_effect=batches) as claim, \
                patch.object(app, "freebie_delivery_allowed", return_value=False), \
                patch.object(app, "defer_scheduled_action", return_value=True), \
                patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app.deliver_due_actions({CHAT}, limit=25), (100, 0, 0))
        self.assertEqual(claim.call_count, 4)
        send.assert_not_called()

    def test_successful_delivery_stays_within_requested_limit(self):
        actions = [{"id": i, "chat_id": CHAT, "action_type": "message", "payload": {"text": "Offer"}}
                   for i in range(25)]
        with patch.object(app, "claim_scheduled_actions", return_value=actions) as claim, \
                patch.object(app, "send_scheduled_action", return_value={"message_id": 900}) as send, \
                patch.object(app, "finish_scheduled_action", return_value=True), \
                patch.object(app, "save_update"):
            self.assertEqual(app.deliver_due_actions({CHAT}, limit=25), (25, 25, 0))
        claim.assert_called_once()
        self.assertEqual(send.call_count, 25)
