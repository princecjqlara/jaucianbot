import copy
import datetime as dt
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import client_workflow as workflow
import cloud_store
import new_client_automation as clients
import ready_availability as ready
import telegram_sender
import trabawho_songs as songs
import manage_webhook
from daily_automation import AVAILABILITY_GROUPS, TRABAWHO_CHAT_ID
from postgres_archive import compile_request
from test_wsgi import call_app
from test_member_performance import offer, report, members, sale, DELIVERED, NOW as REPORT_NOW

CHAT = ready.OLLIE
NOW = dt.datetime(2026, 10, 14, 2, tzinfo=dt.timezone.utc)


def assignment(chat=CHAT, **extra):
    return {"id": 100, "chat_id": chat, "status": "cancelled", "updated_at": NOW.isoformat(),
            "telegram_message_id": 500, "payload": {
                "new_client_token": "AAAABBBB", "new_client_contact_id": "client-1",
                "new_client_contact_name": "Client & Name", "new_client_page": "Page",
                "new_client_assignee_id": 11, "new_client_assignee_name": "Alex",
                "new_client_thread_id": AVAILABILITY_GROUPS[chat]["contact_thread"],
                "new_client_acknowledged_at": NOW.isoformat(), "new_client_phase": "claimed", **extra}}


def callback(data, actor=11, chat=CHAT, thread=None):
    return {"update_id": 900, "callback_query": {"id": "callback-1", "from": {"id": actor, "first_name": "Alex"},
        "data": "cw:" + data, "message": {"message_id": 500, "chat": {"id": chat},
        "message_thread_id": thread or AVAILABILITY_GROUPS[chat]["contact_thread"]}}}


class ClientWorkflowTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("client_workflow.reconcile_row", None), ("client_workflow.is_member", True)):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def save(self, row, payload, now):
        row["payload"] = payload
        return True

    def start(self, row, **extra):
        return workflow.start_handover(row, extra.get("actor", 11), extra.get("target", 22), "Bea", "Send draft after review", NOW,
                                       extra.get("event", "request-1"), extra.get("revision", 0))

    def test_only_owner_can_request_and_self_transfer_is_rejected(self):
        row = assignment()
        with patch.object(workflow, "save_assignment") as save:
            self.assertIn("Only the current editor", self.start(row, actor=33))
            self.assertIn("different editor", self.start(row, target=11))
        save.assert_not_called()

    def test_request_keeps_owner_until_chosen_recipient_accepts(self):
        row = assignment()
        with patch.object(workflow, "save_assignment", side_effect=self.save):
            self.start(row)
            self.assertEqual(row["payload"]["new_client_assignee_id"], 11)
            self.assertIn("another editor", workflow.finish_handover(row, 33, 1, "yes", NOW, "wrong"))
            self.assertEqual(row["payload"]["new_client_assignee_id"], 11)
            workflow.finish_handover(row, 22, 1, "yes", NOW + dt.timedelta(minutes=1), "accepted-1")
        p = row["payload"]
        self.assertEqual(p["new_client_assignee_id"], 22)
        self.assertEqual(p["new_client_initial_assignee_id"], 11)
        self.assertEqual(p["new_client_acknowledged_at"], NOW.isoformat())
        self.assertEqual(p["new_client_handover_events"][0]["note"], "Send draft after review")
        self.assertEqual(p["new_client_handover_revision"], 2)

    def test_decline_or_cancel_keeps_original_owner_and_is_audited(self):
        for decision, actor in (("no", 22), ("cancel", 11)):
            row = assignment()
            with patch.object(workflow, "save_assignment", side_effect=self.save):
                self.start(row)
                workflow.finish_handover(row, actor, 1, decision, NOW, "resolved")
            self.assertEqual(row["payload"]["new_client_assignee_id"], 11)
            self.assertEqual(len(row["payload"]["new_client_handover_events"]), 1)

    def test_only_requester_can_cancel_and_only_receiver_can_decline(self):
        row = assignment()
        with patch.object(workflow, "save_assignment", side_effect=self.save):
            self.start(row)
        with patch.object(workflow, "save_assignment") as save:
            workflow.finish_handover(row, 22, 1, "cancel", NOW, "cancel")
            workflow.finish_handover(row, 11, 1, "no", NOW, "no")
        save.assert_not_called()

    def test_replay_does_not_transfer_twice_or_restart_old_request(self):
        row = assignment()
        with patch.object(workflow, "save_assignment", side_effect=self.save) as save:
            self.start(row)
            self.start(row)
            workflow.finish_handover(row, 22, 1, "yes", NOW, "accept")
            workflow.finish_handover(row, 22, 1, "yes", NOW, "accept")
        self.assertEqual(save.call_count, 2)
        self.assertEqual(len(row["payload"]["new_client_handover_events"]), 1)

    def test_old_picker_and_nonmember_do_not_create_request(self):
        row = assignment(new_client_handover_revision=2)
        with patch.object(workflow, "save_assignment") as save:
            self.start(row)
        save.assert_not_called()
        row = assignment()
        with patch.object(workflow, "is_member", return_value=False), patch.object(workflow, "save_assignment") as save:
            self.assertIn("current member", self.start(row))
        save.assert_not_called()

    def test_parallel_owner_changes_have_one_winner_even_with_equal_timestamps(self):
        stored = assignment()
        lock = threading.Lock()
        def cas(path, payload, **kwargs):
            filters = parse_qs(urlsplit(path).query)
            with lock:
                expected = filters["payload->>new_client_handover_revision"][0]
                actual = stored["payload"].get("new_client_handover_revision")
                if expected != (f"eq.{actual}" if actual is not None else "is.null"):
                    return []
                stored.update(copy.deepcopy(payload))
                return [{"id": 100}]
        rows = [assignment(), assignment()]
        with patch.object(workflow, "request", side_effect=cas):
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda row: workflow.save_assignment(row, dict(row["payload"], new_client_handover_revision=1), NOW), rows))
        self.assertEqual(sum(outcomes), 1)

    def test_postgres_transfer_query_compiles_with_revision_and_latest_guards(self):
        row = assignment()
        with patch.dict("os.environ", {"ARCHIVE_TRANSPORT": "postgres"}), patch.object(workflow, "request", return_value=[{"id": 100}]) as request:
            workflow.save_assignment(row, dict(row["payload"], new_client_handover_revision=1), NOW)
        path, payload = request.call_args.args
        statement, _, _ = compile_request(path, payload, method="PATCH", prefer="return=representation")
        self.assertIn("NOT EXISTS", statement.as_string())

    def test_group_and_topic_scope_rejects_buttons_without_database_reads(self):
        with patch.object(workflow, "full_assignment") as read:
            self.assertIsNone(workflow.handle(callback("yes:100:1"), set(), NOW))
            self.assertIsNone(workflow.handle(callback("yes:100:1", thread=999), {CHAT}, NOW))
        read.assert_not_called()

    def test_all_five_teams_support_buttons_and_readonly_list_commands(self):
        with patch.object(workflow, "queue_message") as queue, patch("suno_store.suno_configured", return_value=True):
            for chat in AVAILABILITY_GROUPS:
                self.assertIn("latest client list", workflow.handle(callback("list:0", chat=chat), {chat}, NOW))
                self.assertEqual(queue.call_args.args[0], chat)

    def test_edited_commands_do_not_create_handovers(self):
        incoming = {"edited_message": {"chat": {"id": CHAT}, "from": {"id": 11}, "text": "/handover AAAABBBB"}}
        self.assertIsNone(workflow.handle(incoming, {CHAT}, NOW))

    def test_picker_checks_owner_and_has_pagination_for_large_teams(self):
        row = assignment()
        with patch.object(workflow, "teammates", return_value={i: f"Editor {i}" for i in range(11, 50)}), patch.object(workflow, "queue_message") as queue:
            workflow.choose(row, 11, 0, 0, NOW, "pick")
        keyboard = queue.call_args.args[4]["inline_keyboard"]
        self.assertEqual(len(keyboard), 9)
        self.assertEqual(keyboard[-1][0]["text"], "Next")
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for line in keyboard for b in line))

    def test_accept_button_uses_clicking_member_and_current_time(self):
        with patch.object(clients, "confirm_new_client_reply", return_value=True) as confirm, patch.object(workflow, "queue_board"):
            workflow.handle(callback("accept:AAAABBBB", actor=22), {CHAT}, NOW)
        msg = confirm.call_args.args[0]["message"]
        self.assertEqual(msg["from"]["id"], 22)
        self.assertEqual(msg["text"], "WORKING AAAABBBB")
        self.assertEqual(msg["date"], int(NOW.timestamp()))

    def test_webhook_callback_is_acknowledged_without_dispatch_or_ingestion(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch.object(workflow, "handle", return_value="Accepted"), patch.object(app, "save_update") as ingest, patch.object(app, "deliver_due_actions") as deliver:
            status, body = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"}, body=json.dumps(callback("accept:AAAABBBB")).encode())
        self.assertEqual(status, 200)
        self.assertEqual(body["method"], "answerCallbackQuery")
        ingest.assert_not_called()
        deliver.assert_not_called()

    def test_callback_failure_is_retried_and_does_not_expose_error(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch.object(workflow, "handle", side_effect=RuntimeError("secret")):
            status, body = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"}, body=json.dumps(callback("yes:100:1")).encode())
        self.assertEqual(status, 500)
        self.assertNotIn("secret", str(body))

    def test_directory_merges_eligible_pool_and_current_owner_once(self):
        row = assignment()
        old = assignment()
        old["id"] = 99
        old["payload"]["new_client_assignee_id"] = 33
        pool = [("Page", [{"id": "client-1", "name": "Client & Name"}, {"id": "client-2", "name": "Available client"}])]
        with patch.object(workflow, "history_for", return_value=[old, row]), patch.object(clients, "_page_contacts", return_value=pool):
            snapshot = workflow.directory(CHAT, NOW)
        self.assertEqual(len(snapshot["clients"]), 2)
        self.assertEqual(snapshot["counts"], {"Assigned": 1, "Available": 1})
        owner = next(c for c in snapshot["clients"] if c["editor_id"])
        self.assertEqual(owner["editor_id"], 11)
        self.assertFalse(owner["available"])

    def test_pending_handover_list_still_names_original_editor(self):
        row = assignment(new_client_handover={"state": "pending", "to_name": "Bea"})
        with patch.object(workflow, "history_for", return_value=[row]), patch.object(clients, "_page_contacts", return_value=[]):
            snapshot = workflow.directory(CHAT, NOW)
        entry = snapshot["clients"][0]
        self.assertEqual(entry["status"], "Handover pending")
        self.assertEqual(entry["editor_id"], 11)
        self.assertEqual(entry["next_editor"], "Bea")
        self.assertFalse(entry["available"])

    def test_directory_pages_fit_telegram_and_escape_client_html(self):
        entries = [{"client": "<&>" * 30, "page": "<&>" * 30, "status": "Available", "editor": None,
                    "contact_id": str(i)} for i in range(55)]
        snapshot = {"clients": entries, "counts": {"Available": 55}, "team": "Veo", "chat_id": CHAT,
                    "as_of_utc": NOW.isoformat(), "coverage": "Tracked clients"}
        text, markup = workflow.render_directory(snapshot, 1, own_view=True)
        self.assertIn("Page 2/5", text)
        self.assertIn("&lt;&amp;&gt;", text)
        self.assertLess(len(__import__('html').unescape(text)), 4096)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "cw:view:0")

    def test_automatic_board_edits_saved_message_and_renders_at_delivery(self):
        action = {"id": 1, "chat_id": CHAT, "telegram_message_id": 321, "payload": {"client_directory": True}}
        snapshot = {"clients": [], "counts": {}, "team": "Veo", "chat_id": CHAT, "as_of_utc": NOW.isoformat(),
                    "coverage": "Tracked clients", "assignment_fingerprint": "fresh"}
        with patch.object(workflow, "directory", return_value=snapshot):
            self.assertTrue(workflow.prepare_delivery(action, NOW))
        self.assertEqual(action["payload"]["edit_message_id"], 321)
        self.assertEqual(action["payload"]["_directory_assignment_fingerprint"], "fresh")

    def test_obsolete_handover_notices_are_suppressed(self):
        row = assignment(new_client_handover={"state": "accepted", "revision": 1})
        action = {"chat_id": CHAT, "payload": {"client_transfer_assignment_id": 100,
                  "client_transfer_revision": 1, "client_transfer_state": "pending"}}
        with patch.object(workflow, "full_assignment", return_value=row):
            self.assertFalse(workflow.prepare_delivery(action, NOW))
            action["payload"]["client_transfer_state"] = "accepted"
            self.assertTrue(workflow.prepare_delivery(action, NOW))

    def test_directory_endpoint_is_authenticated_and_group_scoped(self):
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), patch.object(workflow, "directory", return_value={"clients": []}) as read:
            self.assertEqual(call_app("/api/clients")[0], 401)
            self.assertEqual(call_app("/api/clients", headers={"Authorization": "Bearer test"}, query="group=-100123")[0], 404)
            self.assertEqual(call_app("/api/clients", headers={"Authorization": "Bearer test"}, query=f"group={CHAT}")[0], 200)
        self.assertEqual(read.call_count, 1)

    def test_song_owner_sync_preserves_deadline_and_completion_metadata(self):
        row = assignment(TRABAWHO_CHAT_ID, new_client_assignee_id=22, new_client_handover_events=[
            {"state": "accepted", "resolved_at": NOW.isoformat()}])
        stored = {"id": 700, "updated_at": "previous", "payload": {"song_assignee_id": 11,
            "song_started_at": "start", "song_deadline_at": "original deadline", "_delivery_failures": 0}}
        with patch.object(cloud_store, "request", side_effect=[[stored], [{"id": 700}]]) as request:
            self.assertTrue(cloud_store.sync_song_owner(700, row, NOW))
        p = request.call_args.args[1]["payload"]
        self.assertEqual(p["song_assignee_id"], 22)
        self.assertEqual(p["song_deadline_at"], "original deadline")
        self.assertEqual(p["song_owner_since"], NOW.isoformat())

    def test_handover_preserves_initial_response_and_routes_sales_by_owner_period(self):
        accepted = DELIVERED + dt.timedelta(minutes=1)
        transfer = DELIVERED + dt.timedelta(hours=1)
        row = offer(ack=accepted, user=22, new_client_initial_assignee_id=11, new_client_initial_assignee_name="User 11",
                    new_client_original_assignee_id=11, new_client_handover_events=[{"state": "accepted",
                        "to_id": 22, "to_name": "User 22", "resolved_at": transfer.isoformat()}])
        result = members(report([row], [sale(1, user=11, at=transfer-dt.timedelta(minutes=1)),
                                       sale(2, user=22, at=transfer+dt.timedelta(minutes=1)),
                                       sale(3, user=11, at=transfer+dt.timedelta(minutes=2))]))
        self.assertEqual(result[11]["average_response_minutes"], 1)
        self.assertEqual(result[11]["on_time_acceptances"], 1)
        self.assertEqual(result[11]["linked_dd_posts"], 1)
        self.assertEqual(result[11]["unlinked_dd_posts"], 1)
        self.assertEqual(result[22]["linked_dd_posts"], 1)
        self.assertEqual(result[22]["delivered_offers"], 0)
        self.assertEqual(result[22]["accepted_clients"], 1)

    def test_handover_does_not_mark_original_on_time_editor_as_missing(self):
        row = assignment(new_client_ready_protocol=True, new_client_response_minutes=5,
                         _first_delivery_at=NOW.isoformat(), new_client_assignee_id=22,
                         new_client_original_assignee_id=11, new_client_initial_assignee_id=11,
                         new_client_acknowledged_at=(NOW + dt.timedelta(minutes=1)).isoformat())
        self.assertEqual(ready.missed_deadlines([row], NOW + dt.timedelta(hours=1)), {})

    def test_one_team_failure_does_not_skip_other_team_directory_maintenance(self):
        def read(chat):
            if chat == min(AVAILABILITY_GROUPS):
                raise RuntimeError("private details")
            return []
        with patch.object(workflow, "history_for", side_effect=read), patch.object(workflow, "queue_board") as board, patch("suno_store.suno_configured", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "maintenance failed"):
                workflow.maintain(NOW, set(AVAILABILITY_GROUPS))
        self.assertEqual(board.call_count, 4)

    def test_old_song_owner_cannot_complete_before_job_sync(self):
        job = {"id": 700, "payload": {"song_assignment_id": 100, "song_assignee_id": 11,
                 "song_started_at": NOW.isoformat(), "song_owner_since": NOW.isoformat()}}
        message = {"message_id": 1000, "date": int((NOW + dt.timedelta(minutes=1)).timestamp())}
        with patch.object(songs, "song_assignment_owner", return_value=22), patch.object(songs, "complete_song_job") as complete:
            self.assertFalse(songs._record_completion(message, job, NOW + dt.timedelta(minutes=2)))
        complete.assert_not_called()

    def test_prepared_webhook_change_adds_callbacks_without_dropping_updates(self):
        with patch.object(manage_webhook, "load_local_env"), patch.object(manage_webhook, "require_token", return_value="test"), patch.dict("os.environ", {"TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch("sys.argv", ["manage_webhook.py", "set", "https://example.invalid/api/webhook"]), patch.object(manage_webhook, "api_request", return_value=True) as api, patch("builtins.print"):
            manage_webhook.main()
        params = api.call_args.args[2]
        self.assertIn("callback_query", json.loads(params["allowed_updates"]))
        self.assertEqual(params["drop_pending_updates"], "false")


if __name__ == "__main__":
    unittest.main()
