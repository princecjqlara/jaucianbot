import copy
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import cloud_store
import new_client_automation as clients
from postgres_archive import compile_request
from psycopg._queries import _query2pg
from test_hourly_assignments import CHAT, NOW, offer, message


def attempts():
    older, latest = offer(), offer()
    older["id"] = 100
    older["status"] = "cancelled"
    older["payload"].update(new_client_token="1111AAAA", new_client_phase="expired",
                            new_client_cancelled_reason="volunteer_window_ended_unclaimed",
                            new_client_assigned_at=(NOW - dt.timedelta(hours=1)).isoformat(),
                            _first_delivery_at=(NOW - dt.timedelta(hours=1)).isoformat())
    latest["id"] = 101
    latest["payload"].update(new_client_token="2222BBBB", new_client_assignee_id=22, new_client_assignee_name="Bea")
    latest["telegram_message_id"] = 701
    return older, latest


class OwnershipTests(unittest.TestCase):
    def test_reply_orders_always_keep_latest_offer(self):
        for latest_first in (False, True):
            with self.subTest(latest_first=latest_first):
                older, latest = attempts()
                history = [older, latest]
                updates = [{"message": message(11, 5, "WORKING 1111AAAA")},
                           {"message": message(22, 6, "WORKING 2222BBBB")}]
                if latest_first:
                    updates.reverse()
                with patch.object(clients, "new_client_actions", return_value=history), patch.object(clients, "update_new_client_action", return_value=True) as update, patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(clients, "queue_new_client_assignments"):
                    for incoming in updates:
                        self.assertTrue(clients.confirm_new_client_reply(incoming, {CHAT}))
                self.assertFalse(older["payload"].get("new_client_acknowledged_at"))
                self.assertEqual(latest["payload"]["new_client_assignee_id"], 22)
                self.assertTrue(latest["payload"].get("new_client_acknowledged_at"))
                self.assertEqual(update.call_count, 1)
                self.assertEqual(update.call_args.args[0], 101)
                notices = [call.kwargs for call in enqueue.call_args_list if call.kwargs["dedupe_key"].startswith("new-client-wait:")]
                self.assertEqual(len(notices), 1)
                self.assertIn("wait for your next turn", notices[0]["payload"]["text"])
                self.assertIn("Bea", notices[0]["payload"]["text"])
                self.assertEqual(notices[0]["payload"]["reply_parameters"]["message_id"], 811)

    def test_direct_working_reply_to_old_message_gets_wait_notice(self):
        older, latest = attempts()
        incoming = message(11, 5, "WORKING")
        incoming["reply_to_message"] = {"message_id": older["telegram_message_id"]}
        with patch.object(clients, "new_client_actions", return_value=[older, latest]), patch.object(clients, "update_new_client_action") as update, patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertTrue(clients.confirm_new_client_reply({"message": incoming}, {CHAT}))
        update.assert_not_called()
        self.assertIn("wait for your next turn", enqueue.call_args.kwargs["payload"]["text"])

    def test_same_editor_uses_new_token_instead_of_being_told_to_wait(self):
        older, latest = attempts()
        latest["payload"]["new_client_assignee_id"] = 11
        with patch.object(clients, "new_client_actions", return_value=[older, latest]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertTrue(clients.confirm_new_client_reply({"message": message(11, 5, "WORKING 1111AAAA")}, {CHAT}))
        self.assertIn("latest WORKING token", enqueue.call_args.kwargs["payload"]["text"])
        self.assertNotIn("wait for your next turn", enqueue.call_args.kwargs["payload"]["text"])

    def test_archive_prioritizes_latest_offer_even_if_old_reply_is_earlier(self):
        older, latest = attempts()
        replies = [dict(message(11, 5, "WORKING 1111AAAA"), author_id=11),
                   dict(message(22, 6, "WORKING 2222BBBB"), author_id=22)]
        with patch.object(clients, "new_client_reply_messages", return_value=replies), patch.object(clients, "_active_members", return_value=[]), patch.object(clients, "update_new_client_action", return_value=True) as update, patch.object(clients, "enqueue_scheduled_action", return_value=True):
            clients._reconcile_archived_confirmations([older, latest], NOW + dt.timedelta(minutes=10))
        self.assertEqual(update.call_args.args[0], 101)
        self.assertFalse(older["payload"].get("new_client_acknowledged_at"))
        self.assertTrue(latest["payload"].get("new_client_acknowledged_at"))

    def test_replaced_contact_rows_and_transitive_identity_links_share_current_offer(self):
        older, bridge, latest = offer(), offer(), offer()
        older["id"], bridge["id"], latest["id"] = 100, 101, 102
        older["payload"].pop("new_client_contact_identity")
        older["payload"]["new_client_contact_id"] = "old-id"
        bridge["payload"]["new_client_contact_id"] = "old-id"
        latest["payload"]["new_client_contact_id"] = "new-id"
        current = clients._current_assignments([older, bridge, latest])
        self.assertEqual([current[i]["id"] for i in (100, 101, 102)], [102, 102, 102])

    def test_same_ids_in_other_groups_do_not_take_ownership(self):
        older, latest = attempts()
        latest["chat_id"] = -1003962888977
        current = clients._current_assignments([older, latest])
        self.assertEqual(current[100]["id"], 100)
        self.assertEqual(current[101]["id"], 101)

    def test_existing_wrong_claim_is_revoked_and_latest_archived_reply_recovers(self):
        older, latest = attempts()
        older["payload"]["new_client_acknowledged_at"] = (NOW + dt.timedelta(minutes=5)).isoformat()
        older["payload"]["new_client_ack_message_id"] = 811
        latest["status"] = "cancelled"
        latest["payload"]["new_client_cancelled_reason"] = clients.DUPLICATE_REASON
        latest["payload"].pop("new_client_response_minutes")  # Earlier production's legacy offer
        revoked = dict(older["payload"], new_client_acknowledged_at=None, new_client_superseded_by=101,
                       new_client_cancelled_reason=clients.SUPERSEDED_REASON, new_client_phase="superseded",
                       new_client_retirement_complete=True)
        with patch.object(clients, "retire_new_client_action", return_value={"payload": revoked}) as retire, patch.object(clients, "new_client_reply_messages", return_value=[dict(message(22, 6, "WORKING 2222BBBB"), author_id=22)]), patch.object(clients, "_active_members", return_value=[]), patch.object(clients, "update_new_client_action", return_value=True) as update, patch.object(clients, "enqueue_scheduled_action", return_value=True), patch.object(clients, "restore_new_client_action") as restore:
            clients._reconcile_archived_confirmations([older, latest], NOW + dt.timedelta(minutes=10))
        retire.assert_called_once_with(100, CHAT, 101, NOW + dt.timedelta(minutes=10))
        self.assertEqual(update.call_args.args[0], 101)
        self.assertTrue(update.call_args.kwargs["include_cancelled"])
        self.assertFalse(older["payload"].get("new_client_acknowledged_at"))
        self.assertTrue(latest["payload"].get("new_client_acknowledged_at"))
        restore.assert_not_called()

    def test_wrongly_cancelled_latest_offer_resumes_if_no_reply_yet(self):
        older, latest = attempts()
        older["payload"].update(new_client_superseded_by=101, new_client_retirement_complete=True)
        latest["status"] = "cancelled"
        latest["payload"]["new_client_cancelled_reason"] = clients.DUPLICATE_REASON
        restored = dict(latest["payload"], new_client_cancelled_reason=None, new_client_phase="direct")
        with patch.object(clients, "new_client_reply_messages", return_value=[]), patch.object(clients, "_active_members", return_value=[]), patch.object(clients, "restore_new_client_action", return_value={"payload": restored}) as restore:
            clients._reconcile_archived_confirmations([older, latest], NOW)
        restore.assert_called_once_with(101, CHAT, NOW)
        self.assertEqual(latest["status"], "pending")

    def test_competing_volunteer_reply_keeps_first_claim_same_offer(self):
        row = offer()
        row["payload"].update(new_client_acknowledged_at=NOW.isoformat(), new_client_phase="claimed",
                              new_client_original_assignee_id=11, new_client_assignee_id=22, new_client_assignee_name="Bea")
        with patch.object(clients, "new_client_actions", return_value=[row]), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue, patch.object(clients, "update_new_client_action") as update:
            self.assertTrue(clients.confirm_new_client_reply({"message": message(11, 26, "TAKE AABBCCDD")}, {CHAT}))
        update.assert_not_called()
        self.assertIn("Bea", enqueue.call_args.kwargs["payload"]["text"])

    def test_superseded_acknowledgment_is_not_delivered(self):
        older, latest = attempts()
        older["payload"]["new_client_acknowledged_at"] = NOW.isoformat()
        with patch.object(clients, "new_client_actions", return_value=[older, latest]):
            self.assertFalse(clients.new_client_ack_delivery_allowed({"chat_id": CHAT, "payload": {"new_client_ack_assignment_id": 100}}))


class OwnershipStoreTests(unittest.TestCase):
    def test_driver_can_parse_guarded_confirmation_without_literal_percent_error(self):
        details = dict(offer()["payload"], new_client_acknowledged_at=NOW.isoformat())
        statement, values, _ = compile_request(
            "scheduled_actions?select=id&id=eq.100&status=in.(pending,cancelled)"
            "&payload-%3E%3Enew_client_acknowledged_at=is.null&new_client_latest_guard=true",
            {"payload": details, "status": "cancelled", "updated_at": NOW.isoformat()},
            method="PATCH", prefer="return=representation")
        converted, formats, names, parts = _query2pg(statement.as_string().encode(), "utf-8")
        self.assertEqual(len(formats), len(values))
        self.assertIn("new-client:%", values)
        self.assertNotIn(b"new-client:%", converted)
        self.assertIn(b"newer.dedupe_key LIKE $", converted)

    def test_atomic_confirmation_guard_is_parameterized_and_rejects_newer_offer(self):
        details = offer()["payload"]
        details["new_client_acknowledged_at"] = NOW.isoformat()
        statement, values, _ = compile_request(
            "scheduled_actions?select=id&id=eq.100&new_client_latest_guard=true",
            {"payload": details, "status": "cancelled"}, method="PATCH", prefer="return=representation")
        query = statement.as_string()
        self.assertIn("NOT EXISTS", query)
        self.assertIn("newer.id > a.id", query)
        self.assertIn("newer.chat_id = a.chat_id", query)
        self.assertNotIn("psid:page:person", query)
        self.assertIn("psid:page:person", values)

    def test_ownership_guard_cannot_be_used_for_arbitrary_or_unbounded_updates(self):
        for path, payload in [
            ("scheduled_actions?new_client_latest_guard=true", {"payload": {"new_client_acknowledged_at": NOW.isoformat()}}),
            ("messages?id=eq.100&new_client_latest_guard=true", {"payload": {"new_client_acknowledged_at": NOW.isoformat()}}),
            ("scheduled_actions?id=eq.100&new_client_latest_guard=true", {"payload": {}}),
        ]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                compile_request(path, payload, method="PATCH")

    def test_revoke_keeps_audit_and_full_details_cancels_ack_and_unfinished_song(self):
        stored = dict(offer()["payload"], text="Original details", new_client_collected_details={"brief": "complete"},
                      new_client_acknowledged_at=NOW.isoformat())
        with patch.object(cloud_store, "request", side_effect=[
            [{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 100}], [],
            [{"id": 200}], [{"id": 100}],
        ]) as request, patch.object(cloud_store, "_update_assignment", return_value={"id": 200}) as cancel:
            revoked = cloud_store.retire_new_client_action(100, CHAT, 101, NOW)
        self.assertIsNone(revoked["payload"]["new_client_acknowledged_at"])
        self.assertEqual(revoked["payload"]["new_client_revoked_acknowledged_at"], NOW.isoformat())
        self.assertEqual(revoked["payload"]["new_client_collected_details"], stored["new_client_collected_details"])
        self.assertTrue(revoked["payload"]["new_client_retirement_complete"])
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[1]["song_cancelled_reason"], "assignment_superseded")
        ack_query = parse_qs(urlsplit(request.call_args_list[2].args[0]).query)
        self.assertEqual(ack_query["dedupe_key"], ["eq.new-client-ack:100"])

    def test_revision_conflict_does_not_revoke_current_claim(self):
        stored = dict(offer()["payload"], new_client_acknowledged_at=NOW.isoformat())
        with patch.object(cloud_store, "request", side_effect=[[{"payload": stored, "updated_at": NOW.isoformat()}], []]) as request:
            self.assertIsNone(cloud_store.retire_new_client_action(100, CHAT, 101, NOW))
        self.assertEqual(request.call_count, 2)

    def test_restored_offer_keeps_complete_brief_and_starts_fresh_delivery_timer(self):
        stored = dict(offer()["payload"], text="Hi Bea! Complete brief here.\nWORKING 2222BBBB\n\nPlease reply within 60 minutes.", new_client_cancelled_reason=clients.DUPLICATE_REASON)
        with patch.object(cloud_store, "request", side_effect=[[{"payload": stored, "updated_at": NOW.isoformat()}], [{"id": 101}]]) as request:
            restored = cloud_store.restore_new_client_action(101, CHAT, NOW)
        self.assertIn("Complete brief here", restored["payload"]["text"])
        self.assertIn("20 minutes", restored["payload"]["text"])
        self.assertNotIn("60 minutes", restored["payload"]["text"])
        self.assertIsNone(restored["payload"]["_first_delivery_at"])
        self.assertIsNone(request.call_args.args[1]["sent_at"])
        self.assertEqual(request.call_args.args[1]["status"], "pending")

    def test_archive_recovers_take_mine_and_working_while_ignoring_chatter(self):
        rows = [{"message_id": i, "text": text} for i, text in enumerate(
            ["hello", "TAKE AABBCCDD", "MINE AABBCCDD", "WORKING AABBCCDD", "thanks"])]
        with patch.object(cloud_store, "request", return_value=rows):
            found = cloud_store.new_client_reply_messages(CHAT, 4180, NOW, NOW, author_ids={11, 22})
        self.assertEqual([r["message_id"] for r in found], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
