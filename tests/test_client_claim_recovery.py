import copy
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import client_volunteers as volunteers
import cloud_store
import new_client_automation as clients
from test_hourly_assignments import CHAT, NOW, MEMBERS, offer, message
from test_client_ownership import attempts


class ClaimRecoveryTests(unittest.TestCase):
    def test_concurrent_loser_reads_winner_and_gets_wait_reply(self):
        initial = offer()
        winner = copy.deepcopy(initial)
        winner["status"] = "cancelled"
        winner["payload"].update(new_client_acknowledged_at=NOW.isoformat(),
                                 new_client_phase="claimed", new_client_assignee_id=22,
                                 new_client_assignee_name="Bea")
        with patch.object(clients, "new_client_actions", side_effect=[[initial], [winner]]), \
                patch.object(clients, "_confirms_assignment", return_value=True), \
                patch.object(clients, "update_new_client_action", return_value=None) as update, \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue, \
                patch.object(clients, "queue_new_client_assignments") as queue:
            self.assertTrue(clients.confirm_new_client_reply({"message": message(11, 26, "TAKE AABBCCDD")}, {CHAT}))
        self.assertEqual(update.call_count, 1)
        queue.assert_not_called()
        self.assertEqual(winner["payload"]["new_client_assignee_id"], 22)
        self.assertIn("wait for your next turn", enqueue.call_args.kwargs["payload"]["text"])
        self.assertIn("Bea", enqueue.call_args.kwargs["payload"]["text"])

    def test_revision_conflict_without_winner_stays_unclaimed(self):
        row = offer()
        with patch.object(clients, "new_client_actions", return_value=[row]), \
                patch.object(clients, "update_new_client_action", return_value=None), \
                patch.object(clients, "enqueue_scheduled_action") as enqueue:
            self.assertFalse(clients.confirm_new_client_reply({"message": message()}, {CHAT}))
        enqueue.assert_not_called()
        self.assertFalse(row["payload"].get("new_client_acknowledged_at"))

    def test_new_offer_created_during_claim_gets_priority(self):
        initial = offer()
        latest = copy.deepcopy(initial)
        latest["id"] = 101
        latest["payload"].update(new_client_token="2222BBBB", new_client_assignee_id=22,
                                 new_client_assignee_name="Bea")
        with patch.object(clients, "new_client_actions", side_effect=[[initial], [initial, latest]]), \
                patch.object(clients, "update_new_client_action", return_value=None), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertTrue(clients.confirm_new_client_reply({"message": message()}, {CHAT}))
        self.assertEqual(enqueue.call_args.kwargs["dedupe_key"], "new-client-wait:101:11")
        self.assertFalse(initial["payload"].get("new_client_acknowledged_at"))

    def test_superseded_confirmation_is_not_requeued(self):
        older, latest = attempts()
        older["payload"].update(new_client_acknowledged_at=NOW.isoformat(), new_client_ack_pending=True)
        revoked = dict(older["payload"], new_client_acknowledged_at=None, new_client_superseded_by=101,
                       new_client_retirement_complete=True, new_client_phase="superseded")
        with patch.object(clients, "retire_new_client_action", return_value={"payload": revoked}), \
                patch.object(clients, "new_client_reply_messages", return_value=[]), \
                patch.object(clients, "_active_members", return_value=[]), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue, \
                patch.object(clients, "mark_new_client_ack_queued") as mark:
            clients._reconcile_archived_confirmations([older, latest], NOW)
        mark.assert_not_called()
        self.assertTrue(all(not call.kwargs["dedupe_key"].startswith("new-client-ack:")
                            for call in enqueue.call_args_list))

    def test_archived_competing_reply_gets_notice_after_owner_saved(self):
        row = offer()
        row["status"] = "cancelled"
        row["payload"].update(new_client_acknowledged_at=(NOW + dt.timedelta(minutes=25)).isoformat(),
                               new_client_phase="claimed", new_client_assignee_id=22, new_client_assignee_name="Bea")
        replies = [message(11, 24, "TAKE AABBCCDD"), message(11, 26, "TAKE AABBCCDD"),
                   message(22, 27, "TAKE AABBCCDD")]
        with patch.object(clients, "new_client_reply_messages", return_value=replies), \
                patch.object(clients, "_active_members", return_value=MEMBERS), \
                patch.object(clients, "update_new_client_action") as update, \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            clients._reconcile_archived_confirmations([row], NOW + dt.timedelta(minutes=30))
        update.assert_not_called()
        self.assertEqual(enqueue.call_count, 1)
        self.assertIn("new-client-wait:", enqueue.call_args.kwargs["dedupe_key"])
        self.assertEqual(row["payload"]["new_client_assignee_id"], 22)

    def test_saved_claim_recovers_confirmation_after_enqueue_failure(self):
        row = offer()
        saved = {}

        def save(action_id, payload, **kwargs):
            saved.update(copy.deepcopy(payload))
            return True

        with patch.object(clients, "update_new_client_action", side_effect=save), \
                patch.object(clients, "enqueue_scheduled_action", side_effect=RuntimeError("outbox unavailable")):
            with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
                clients._record_confirmation(row, message(), NOW + dt.timedelta(minutes=5))
        self.assertTrue(saved["new_client_ack_pending"])
        self.assertTrue(saved["new_client_acknowledged_at"])
        row["payload"] = saved
        with patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue, \
                patch.object(clients, "mark_new_client_ack_queued", return_value=True) as mark, \
                patch.object(clients, "new_client_reply_messages", return_value=[]), \
                patch.object(clients, "_active_members", return_value=[]), \
                patch.object(clients, "update_new_client_action") as update:
            clients._reconcile_archived_confirmations([row], NOW + dt.timedelta(minutes=10))
        self.assertEqual(enqueue.call_args.kwargs["dedupe_key"], "new-client-ack:100")
        mark.assert_called_once_with(100, NOW + dt.timedelta(minutes=10))
        update.assert_not_called()
        self.assertFalse(row["payload"]["new_client_ack_pending"])

    def test_existing_outbox_confirmation_clears_flag_without_duplicate(self):
        row = offer()
        row["payload"].update(new_client_acknowledged_at=NOW.isoformat(), new_client_ack_pending=True)
        with patch.object(clients, "enqueue_scheduled_action", return_value=False) as enqueue, \
                patch.object(clients, "mark_new_client_ack_queued", return_value=True) as mark, \
                patch.object(clients, "new_client_reply_messages", return_value=[]), \
                patch.object(clients, "_active_members", return_value=[]):
            clients._reconcile_archived_confirmations([row], NOW)
        self.assertEqual(enqueue.call_count, 1)
        mark.assert_called_once()
        self.assertFalse(row["payload"]["new_client_ack_pending"])

    def test_retirement_is_repeated_if_old_claim_reappears(self):
        older, latest = attempts()
        older["payload"].update(new_client_superseded_by=101, new_client_retirement_complete=True,
                                 new_client_acknowledged_at=NOW.isoformat())
        revoked = dict(older["payload"], new_client_acknowledged_at=None)
        with patch.object(clients, "retire_new_client_action", return_value={"payload": revoked}) as retire, \
                patch.object(clients, "enqueue_scheduled_action", return_value=True):
            clients._retire_superseded_claims([older, latest], NOW)
        retire.assert_called_once()
        self.assertFalse(older["payload"].get("new_client_acknowledged_at"))

    def test_store_clears_reappeared_ack_for_same_latest_assignment(self):
        stored = dict(offer()["payload"], new_client_superseded_by=101,
                      new_client_retirement_complete=True, new_client_acknowledged_at=NOW.isoformat())
        with patch.object(cloud_store, "request", side_effect=[
            [{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 100}], [], [], [{"id": 100}],
        ]) as request:
            retired = cloud_store.retire_new_client_action(100, CHAT, 101, NOW)
        self.assertIsNone(retired["payload"]["new_client_acknowledged_at"])
        self.assertEqual(request.call_args_list[1].args[1]["status"], "cancelled")

    def test_confirmation_marker_preserves_payload_and_revision_guard(self):
        stored = dict(offer()["payload"], text="Complete private brief", new_client_ack_pending=True,
                      new_client_acknowledged_at=NOW.isoformat())
        with patch.object(cloud_store, "request", side_effect=[
            [{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 100}],
        ]) as request:
            self.assertTrue(cloud_store.mark_new_client_ack_queued(100, NOW))
        update = request.call_args
        self.assertFalse(update.args[1]["payload"]["new_client_ack_pending"])
        self.assertEqual(update.args[1]["payload"]["text"], "Complete private brief")
        query = parse_qs(urlsplit(update.args[0]).query)
        self.assertEqual(query["updated_at"], [f"eq.{NOW.isoformat()}"])


class VolunteerRecoveryTests(unittest.TestCase):
    def test_restored_offer_does_not_reuse_old_notice_timer(self):
        stored = dict(offer()["payload"], text="Brief\nPlease reply within 20 minutes.",
                      new_client_cancelled_reason=clients.DUPLICATE_REASON, new_client_offer_generation=3)
        with patch.object(cloud_store, "request", side_effect=[
            [{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 100}],
        ]):
            restored = cloud_store.restore_new_client_action(100, CHAT, NOW)
        row = offer()
        row["payload"] = restored["payload"]
        self.assertEqual(row["payload"]["new_client_offer_generation"], 4)
        with patch.object(volunteers, "request", return_value=[]) as request:
            self.assertIsNone(volunteers.start(row))
        query = parse_qs(urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query["dedupe_key"], ["like.volunteer-notice:100:generation-4:*"])
        with patch.object(clients, "_active_members", return_value=MEMBERS), \
                patch.object(volunteers, "enqueue_scheduled_action", return_value=True) as enqueue, \
                patch.object(volunteers, "update_new_client_action", return_value=True):
            volunteers.open_window(row, NOW + dt.timedelta(minutes=20))
        self.assertEqual(enqueue.call_args.kwargs["dedupe_key"], "volunteer-notice:100:generation-4:0")
        self.assertEqual(enqueue.call_args.kwargs["payload"]["volunteer_generation"], 4)

    def test_old_generation_notice_is_suppressed(self):
        row = offer()
        row["payload"].update(new_client_phase="volunteer", new_client_offer_generation=2)
        action = {"payload": {"volunteer_parent_id": 100, "volunteer_generation": 1}}
        with patch.object(volunteers, "request", return_value=[row]):
            self.assertEqual(volunteers.delivery_state(action, NOW), "suppress")

    def test_missing_volunteer_invitation_is_recreated(self):
        row = offer()
        row["status"] = "cancelled"
        row["payload"].update(new_client_phase="volunteer", new_client_cancelled_reason=volunteers.OPEN)
        with patch.object(volunteers, "notices", return_value=[]), \
                patch.object(clients, "_active_members", return_value=MEMBERS), \
                patch.object(volunteers, "enqueue_scheduled_action", return_value=True) as enqueue, \
                patch.object(volunteers, "update_new_client_action", return_value=True) as update:
            self.assertTrue(volunteers.release(row, NOW + dt.timedelta(minutes=20)))
        self.assertEqual(enqueue.call_count, 1)
        self.assertTrue(update.call_args.kwargs["include_cancelled"])
        self.assertEqual(row["payload"]["new_client_phase"], "volunteer")


if __name__ == "__main__":
    unittest.main()
