import datetime as dt
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import client_workflow
import cloud_store
import crm_store
import new_client_automation as clients
import page_access
import ready_availability as ready
import veo_standby as standby
from daily_automation import GROUPS, TRABAWHO_CHAT_ID
from test_veo_ready_policy import person, offer, NOW
from page_access_fixture import granted_access

CHAT = -1003647732254
OTHER = -1004423057797
ENV = {"VEO_STANDBY_BOT_ENABLED": "true", "VEO_NEW_CLIENTS_ENABLED": "true", "NEW_CLIENT_READY_ENABLED": "true"}


def contact(number="c"):
    return {"id": number, "page_id": "p", "name": number, "psid": number,
        "chatbot_enabled": False, "collected_details": {"request": "video"}}


def state(**changes):
    return {"id": "c", "page_id": "p", "chatbot_enabled": False, "pipeline_stage": "qualified",
        "pipeline_stage_source": "chatbot", "automation_epoch": 4, "status": "stopped",
        "stop_reason": "details_collected", "pause_kind": None, "last_human_activity_at": None,
        "page_enabled": True, "trial_mode_enabled": False, "trial_contact_id": None, **changes}


class StandbyBotTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict("os.environ", {**ENV, "AUTOMATION_CHAT_IDS": f"{CHAT},{OTHER}"}))

    def reconcile(self, *, available=None, access=None, history=None, row=None, contacts=None):
        conn = MagicMock()
        with patch.object(standby, "connect", return_value=conn), \
                patch.object(cloud_store, "new_client_actions", return_value=history or []), \
                patch.object(ready, "available", side_effect=lambda chat, *a: (available or {}).get(chat, [])), \
                patch.object(page_access, "members", side_effect=lambda chat: (access or {}).get(chat, {})), \
                patch.object(standby, "locked_contact", return_value=state(**(row or {}))) as lock, \
                patch.object(standby, "write_permission", return_value=5) as change:
            count = standby.reconcile({CHAT: [("Page", contacts or [contact()])]}, {CHAT, OTHER}, NOW)
        return count, change, lock, conn

    def test_no_ready_and_unowned_standby_enables_only_that_contact_and_records_ownership(self):
        count, change, lock, conn = self.reconcile()
        self.assertEqual(count, 1)
        self.assertTrue(change.call_args.args[2])
        lock.assert_called_once_with(conn.__enter__.return_value, "p", "c")
        query, params = conn.__enter__.return_value.execute.call_args.args
        self.assertIn("telegram_client_standby", query)
        self.assertEqual(params, ("c", "p", 5, NOW))

    def test_another_team_ready_for_shared_page_prevents_bot_enable(self):
        count, change, _, _ = self.reconcile(available={OTHER: [{"user_id": 22}]}, access={OTHER: {22: {"p"}}})
        self.assertEqual(count, 0)
        change.assert_not_called()

    def test_ready_without_access_does_not_claim_a_page_they_cannot_handle(self):
        count, _, _, _ = self.reconcile(available={OTHER: [{"user_id": 22}]}, access={OTHER: {22: {"other-page"}}})
        self.assertEqual(count, 1)

    def test_reserved_and_accepted_clients_never_resume_even_with_no_ready(self):
        for accepted in (False, True):
            held = offer(OTHER, accepted=accepted)
            held["payload"].update(new_client_contact_id="c", new_client_contact_page_id="p")
            count, change, _, _ = self.reconcile(history=[held])
            self.assertEqual(count, 0)
            change.assert_not_called()

    def test_human_holds_opt_outs_closed_outcomes_and_page_settings_are_preserved(self):
        cases = [{"chatbot_enabled": True}, {"page_enabled": False}, {"trial_mode_enabled": True},
            {"pause_kind": "explicit_hold"}, {"pause_kind": "human_takeover"},
            {"last_human_activity_at": NOW}, {"pipeline_stage_source": "manual"},
            {"pipeline_stage_source": "messenger"}, {"stop_reason": "manual"},
            {"stop_reason": "opt_out"}, {"status": "active"}]
        cases += [{"pipeline_stage": stage} for stage in ("paid", "converted", "opted_out", "not_qualified", "order_created")]
        for changes in cases:
            with self.subTest(changes=changes):
                count, change, _, _ = self.reconcile(row=changes)
                self.assertEqual(count, 0)
                change.assert_not_called()

    def test_contact_already_on_never_enters_permission_update_loop(self):
        c = {**contact(), "chatbot_enabled": True}
        count, change, lock, _ = self.reconcile(contacts=[c])
        self.assertEqual(count, 0)
        change.assert_not_called()
        lock.assert_not_called()

    def test_update_batch_is_bounded_and_duplicate_pages_count_contact_once(self):
        count, change, _, _ = self.reconcile(contacts=[contact(str(i)) for i in range(40)])
        self.assertEqual(count, standby.MAX_CHANGES)
        self.assertEqual(change.call_count, standby.MAX_CHANGES)

    def test_offer_restores_only_permission_enabled_by_this_feature_before_enqueue(self):
        conn = MagicMock()
        events = []
        action = {"chat_id": CHAT, "payload": {"new_client_contact_id": "c", "new_client_contact_page_id": "p"}}
        with patch.object(standby, "connect", return_value=conn), \
                patch.object(standby, "locked_contact", return_value=state(chatbot_enabled=True)), \
                patch.object(standby, "pause_ours", side_effect=lambda *a: events.append("pause")):
            result = standby.enqueue_offer(lambda **a: events.append("enqueue") or True, now=NOW, **action)
        self.assertTrue(result)
        self.assertEqual(events, ["pause", "enqueue"])

    def test_failed_or_duplicate_enqueue_rolls_back_database_permission_change(self):
        conn = MagicMock()
        action = {"chat_id": CHAT, "payload": {"new_client_contact_id": "c", "new_client_contact_page_id": "p"}}
        with patch.object(standby, "connect", return_value=conn), \
                patch.object(standby, "locked_contact", return_value=state()), patch.object(standby, "pause_ours"):
            self.assertFalse(standby.enqueue_offer(lambda **a: False, now=NOW, **action))
            conn.__enter__.return_value.rollback.assert_called_once()
            with self.assertRaises(RuntimeError):
                standby.enqueue_offer(MagicMock(side_effect=RuntimeError("enqueue failed")), now=NOW, **action)
        self.assertIsNotNone(conn.__exit__.call_args.args[0])

    def test_locked_or_missing_contact_cannot_be_offered(self):
        enqueue = MagicMock()
        with patch.object(standby, "connect", return_value=MagicMock()), patch.object(standby, "locked_contact", return_value=None):
            self.assertFalse(standby.enqueue_offer(enqueue, now=NOW, chat_id=CHAT,
                payload={"new_client_contact_id": "c", "new_client_contact_page_id": "p"}))
        enqueue.assert_not_called()

    def test_newer_human_permission_epoch_is_never_reversed(self):
        for epoch, expected in ((4, 1), (5, 0)):
            conn = MagicMock()
            conn.execute.return_value.fetchone.return_value = {"original_enabled": False, "resumed_epoch": 4}
            with patch.object(standby, "write_permission") as change:
                standby.pause_ours(conn, state(chatbot_enabled=True, automation_epoch=epoch), NOW)
            self.assertEqual(change.call_count, expected)
            if expected:
                self.assertFalse(change.call_args.args[2])

    def test_table_changes_invalidate_old_replies_without_resetting_briefs_or_creating_messages(self):
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = {"automation_epoch": 5}
        self.assertEqual(standby.write_permission(conn, state(), True, NOW), 5)
        queries = "\n".join(c.args[0].lower() for c in conn.execute.call_args_list)
        self.assertIn("chatbot_enabled", queries)
        self.assertIn("automation_epoch", queries)
        self.assertIn("status='cancelled'", queries)
        self.assertNotIn("collected_details", queries)
        self.assertNotIn("insert", queries)

    def test_database_status_is_read_only_and_failure_does_not_imply_success(self):
        conn = MagicMock()
        conn.__enter__.return_value.execute.return_value.fetchone.return_value = {"ready": True}
        with patch.object(standby, "connect", return_value=conn):
            self.assertTrue(standby.database_available())
        queries = "\n".join(c.args[0].lower() for c in conn.__enter__.return_value.execute.call_args_list)
        self.assertIn("set transaction read only", queries)
        self.assertNotIn("update", queries)
        with patch.object(standby, "connect", side_effect=RuntimeError("unavailable")):
            self.assertFalse(standby.database_available())

    def test_disabled_feature_and_suno_do_not_use_veobot_database(self):
        with patch.object(standby, "connect") as connect:
            self.assertTrue(standby.enqueue_offer(lambda **a: True, now=NOW, chat_id=TRABAWHO_CHAT_ID, payload={}))
            with patch.dict("os.environ", {"VEO_STANDBY_BOT_ENABLED": "false"}):
                self.assertEqual(standby.reconcile({}, {CHAT}, NOW), 0)
                self.assertTrue(standby.prepare_offer({"chat_id": CHAT}, NOW))
        connect.assert_not_called()

    def test_no_ready_queue_still_reads_standby_clients_and_runs_recovery(self):
        pages = [("Page", [contact()])]
        with patch.object(cloud_store, "allowed_chat_ids", return_value={CHAT}), \
                patch.object(clients, "new_client_actions", return_value=[]), \
                patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "_release_stale_assignments"), \
                patch.object(clients.client_handoffs, "resume_missed_offers"), \
                patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=[]), \
                patch.object(clients, "_page_contacts", return_value=pages) as source, \
                patch.object(standby, "reconcile", return_value=1) as recover, \
                patch.object(clients, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {CHAT}), 0)
        source.assert_called_once()
        recover.assert_called_once_with({CHAT: pages}, {CHAT}, NOW)
        enqueue.assert_not_called()

    def test_list_shows_standby_and_ready_busy_controls_without_assigning_anyone(self):
        with patch.object(client_workflow, "history_for", return_value=[]), \
                patch.object(clients, "_page_contacts", return_value=[("Page", [contact()])]), \
                patch.object(ready, "available", return_value=[]), \
                patch.object(page_access, "page_map", return_value={"Page": "p"}), \
                patch.object(page_access, "members", return_value={}):
            data = client_workflow.directory(CHAT, NOW)
        text, buttons = client_workflow.render_directory(data)
        self.assertIn("Standby", text)
        self.assertIn("30 minutes", text)
        self.assertIn("cw:ready", str(buttons))
        self.assertIn("cw:busy", str(buttons))


class VeoQueuePolicyTests(unittest.TestCase):
    def setUp(self):
        granted_access(self)
        env = patch.dict("os.environ", {**ENV, "VEO_STANDBY_BOT_ENABLED": "false"})
        env.start()
        self.addCleanup(env.stop)

    def test_veo_ready_expires_at_thirty_minutes_and_suno_keeps_sixty(self):
        for chat in GROUPS:
            with patch.object(ready, "records", return_value=[person(chat)]):
                self.assertEqual(len(ready.available(chat, NOW + dt.timedelta(minutes=29, seconds=59), [])), 1)
                self.assertEqual(ready.available(chat, NOW + dt.timedelta(minutes=30), []), [])
            self.assertEqual(ready.reassignment_minutes(chat), 15)
        self.assertEqual(ready.ready_minutes(TRABAWHO_CHAT_ID), 60)
        self.assertEqual(ready.reassignment_minutes(TRABAWHO_CHAT_ID), 10)

    def test_solo_ready_is_offered_without_waiting_for_busy_teammates(self):
        self.queue_scenario(two_ready=False)

    def test_ready_again_cannot_jump_other_ready_members_on_fresh_or_missed_client(self):
        for retry in (False, True):
            self.queue_scenario(two_ready=True, retry=retry)

    def queue_scenario(self, *, two_ready, retry=False):
        prior = offer(CHAT, user=11, accepted=True)
        prior["payload"].update(new_client_contact_id="prior", new_client_contact_name="prior")
        history = [prior]
        if retry:
            missed = offer(CHAT, user=44)
            missed.update(id=102, status="cancelled")
            missed["payload"].update(new_client_contact_id="c", new_client_phase="expired", new_client_cancelled_reason=clients.volunteers.EXPIRED)
            history.append(missed)
        people = [person(CHAT, 11, minutes=2)] + ([person(CHAT, 22, minutes=3)] if two_ready else [])
        users = [{"chat_id": CHAT, "user_id": p["payload"]["user_id"], "user_name": p["payload"]["user_name"],
            "updated_at": p["payload"]["ready_at"], "ready_record": p} for p in people]
        with patch.object(clients, "new_client_actions", return_value=history), \
                patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "_release_stale_assignments"), \
                patch.object(clients.client_handoffs, "resume_missed_offers"), patch.object(clients.client_handoffs, "ended", return_value=True), \
                patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=users), \
                patch.object(ready, "reserve", return_value=True), patch.object(clients, "_page_contacts", return_value=[("Page", [contact()])]), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=20), {CHAT}), 1)
        p = queue.call_args.kwargs["payload"]
        self.assertEqual(p["new_client_assignee_id"], 22 if two_ready else 11)
        self.assertEqual(p["new_client_response_minutes"], 15)


if __name__ == "__main__":
    unittest.main()
