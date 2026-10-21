import copy
import datetime as dt
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import app
import client_handoffs as handoffs
import client_volunteers as volunteers
import client_workflow as workflow
import cloud_store
import new_client_automation as clients
import page_access as access
import ready_availability as ready
from daily_automation import GROUPS, TRABAWHO_CHAT_ID
from test_veo_ready_policy import NOW, offer, person

CHAT = ready.OLLIE
PAGES = list(GROUPS[CHAT]["crm_pages"].items())


class PageAccessTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def poll(self):
        return {"chat_id": CHAT, "payload": {"page_access_version": access.version(CHAT),
                "page_ids": [pid for _, pid in PAGES]}}

    def update(self, options, uid=101):
        return {"update_id": uid, "poll_answer": {"poll_id": "poll", "option_ids": options,
                "user": {"id": 11, "first_name": "Alex"}}}

    def test_all_six_teams_get_public_page_poll_in_new_clients(self):
        for chat, config in GROUPS.items():
            with patch.object(access, "enqueue_scheduled_action", return_value=True) as queue:
                self.assertEqual(access.queue_poll(chat, NOW), 1)
            p = queue.call_args.kwargs["payload"]
            self.assertEqual(p["options"], list(config["crm_pages"]) + ["No access"])
            self.assertEqual(p["message_thread_id"], config["contact_thread"])
            self.assertTrue(p["allows_multiple_answers"])
            self.assertFalse(p["is_anonymous"])
        with patch.object(access, "enqueue_scheduled_action") as queue:
            self.assertEqual(access.queue_poll(TRABAWHO_CHAT_ID, NOW), 0)
            queue.assert_not_called()

    def test_no_access_and_withdrawal_clear_pages_and_multiple_choices_are_saved(self):
        for choices, expected in (([0, 1], [p for _, p in PAGES]), ([0, 2], []), ([], [])):
            with patch.object(access, "context", return_value=self.poll()), patch.object(access, "request", return_value=[{"id": 1}]) as save:
                self.assertTrue(access.save_answer(self.update(choices), {CHAT}))
            self.assertEqual(save.call_args.args[1]["payload"]["page_ids"], expected)
            self.assertEqual(save.call_args.args[1]["status"], "cancelled")

    def test_invalid_or_anonymous_answers_never_grant_access(self):
        for choices in ([3], [-1], [True], ["0"]):
            with patch.object(access, "context", return_value=self.poll()), patch.object(access, "request") as save:
                self.assertFalse(access.save_answer(self.update(choices), {CHAT}))
                save.assert_not_called()
        update = self.update([0])
        update["poll_answer"].pop("user")
        with patch.object(access, "context", return_value=self.poll()), patch.object(access, "request") as save:
            self.assertFalse(access.save_answer(update, {CHAT}))
            save.assert_not_called()

    def test_old_update_cannot_restore_withdrawn_access(self):
        row = {"id": 1, "updated_at": NOW.isoformat(), "payload": {"update_id": 200, "page_ids": []}}
        with patch.object(access, "context", return_value=self.poll()), patch.object(access, "request", side_effect=[[], [row]]) as save:
            self.assertTrue(access.save_answer(self.update([0], uid=199), {CHAT}))
        self.assertEqual(save.call_count, 2)

    def test_new_vote_retries_revision_conflict_and_replaces_pages(self):
        row = {"id": 1, "updated_at": NOW.isoformat(), "payload": {"update_id": 100}}
        with patch.object(access, "context", return_value=self.poll()), patch.object(access, "request", side_effect=[[], [row], [], [row], [{"id": 1}]]) as save:
            self.assertTrue(access.save_answer(self.update([1]), {CHAT}))
        self.assertEqual(save.call_args.args[1]["payload"]["page_ids"], [PAGES[1][1]])
        self.assertIn("updated_at", parse_qs(urlsplit(save.call_args.args[0]).query))

    def test_old_page_configuration_cannot_grant_current_access(self):
        poll = self.poll()
        poll["payload"]["page_access_version"] = "old"
        with patch.object(access, "context", return_value=poll), patch.object(access, "request") as save:
            self.assertTrue(access.save_answer(self.update([0]), {CHAT}))
            save.assert_not_called()

    def test_page_access_poll_does_not_get_ingested_as_attendance(self):
        with patch.object(access, "save_answer", return_value=True), patch("hourly_availability.save_answer") as hourly, patch.object(cloud_store, "request") as daily:
            self.assertTrue(cloud_store.save_poll_answer(self.update([0]), {CHAT}))
            hourly.assert_not_called()
            daily.assert_not_called()

    def test_poll_registration_precedes_finalization_and_retry_does_not_resend(self):
        action = {"id": 1, "chat_id": CHAT, "action_type": "poll", "payload": {
            "page_access_version": access.version(CHAT), "page_access_ids": [PAGES[0][1]]}}
        events = []
        with patch.object(access, "poll_record", return_value=None), patch.object(access, "register_poll", side_effect=lambda *a: events.append("register") or True), \
                patch.object(app, "send_scheduled_action", return_value={"message_id": 700, "poll": {"id": "poll"}}), \
                patch.object(app, "finish_scheduled_action", side_effect=lambda *a, **k: events.append("finish") or True), patch.object(app, "save_update"):
            self.assertEqual(app._deliver_claimed_actions([action], {CHAT}), (1, 1, 0))
        self.assertEqual(events, ["register", "finish"])
        with patch.object(access, "poll_record", return_value={"message_id": 700}), patch.object(app, "finish_scheduled_action", return_value=True), patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app._deliver_claimed_actions([action], {CHAT}), (1, 0, 0))
            send.assert_not_called()

    def assign(self, members, permissions, contacts, history=None):
        with patch.object(clients, "new_client_actions", return_value=history or []), \
                patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "_release_stale_assignments"), \
                patch.object(handoffs, "resume_missed_offers"), patch.object(handoffs, "ended", return_value=True), \
                patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=members), \
                patch.object(ready, "reserve", return_value=True), patch.object(access, "members", return_value=permissions), \
                patch.object(clients, "_page_contacts", return_value=contacts), patch.object(clients, "enqueue_scheduled_action", return_value=True) as queue:
            clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=20), {CHAT})
        return [c.kwargs["payload"] for c in queue.call_args_list]

    def users(self):
        return [{"user_id": uid, "user_name": str(uid), "updated_at": (NOW + dt.timedelta(minutes=i + 2)).isoformat(),
                 "ready_record": person(CHAT, uid)} for i, uid in enumerate((22, 33))]

    def contacts(self, index=0, cid="fresh"):
        name, pid = PAGES[index]
        return [(name, [{"id": cid, "page_id": pid, "name": "New client " + cid, "collected_details": {"request": "ad"}}])]

    def test_unanswered_or_no_access_blocks_assignment_and_other_page_is_not_offered(self):
        for permissions in ({}, {22: set()}, {22: {PAGES[1][1]}}):
            self.assertEqual(self.assign(self.users()[:1], permissions, self.contacts(0)), [])
        offers = self.assign(self.users()[:1], {22: {PAGES[0][1]}}, self.contacts(0))
        self.assertEqual(offers[0]["new_client_assignee_id"], 22)
        self.assertEqual(offers[0]["new_client_contact_page_id"], PAGES[0][1])

    def test_reassignment_selects_earliest_ready_member_with_matching_page(self):
        missed = offer(CHAT)
        missed.update(status="cancelled")
        missed["payload"].update(new_client_contact_name="New client missed", new_client_page=PAGES[0][0],
            new_client_contact_page_id=PAGES[0][1], new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        offers = self.assign(self.users(), {22: {PAGES[1][1]}, 33: {PAGES[0][1]}}, self.contacts(0, "missed"), [missed])
        self.assertEqual(offers[0]["new_client_assignee_id"], 33)
        self.assertEqual(offers[0]["new_client_response_minutes"], 10)

    def test_a_member_without_access_to_available_page_does_not_block_its_round_robin(self):
        old = offer(CHAT, user=22, accepted=True)
        old["payload"].update(new_client_contact_name="Old owned", new_client_contact_id="old-owned")
        offers = self.assign(self.users(), {22: {PAGES[0][1]}, 33: {PAGES[1][1]}}, self.contacts(0), [old])
        self.assertEqual(offers[0]["new_client_assignee_id"], 22)

    def test_access_withdrawal_blocks_first_delivery_and_acceptance(self):
        row = offer(CHAT)
        row.update(sent_at=None)
        row["payload"].update(_first_delivery_at=None, new_client_page=PAGES[0][0], new_client_contact_page_id=PAGES[0][1])
        with patch.object(access, "members", return_value={}), patch.object(clients, "update_new_client_action", return_value=True) as cancel:
            self.assertFalse(clients.new_client_delivery_allowed(row, NOW))
            self.assertEqual(cancel.call_args.args[1]["new_client_cancelled_reason"], "assignee_inactive_before_first_delivery")
        row["payload"]["_first_delivery_at"] = NOW.isoformat()
        msg = {"from": {"id": 11}, "date": int((NOW + dt.timedelta(minutes=1)).timestamp()), "text": "WORKING AABBCCDD"}
        with patch.object(access, "members", return_value={}):
            self.assertFalse(clients._confirms_assignment(msg, row))

    def test_any_missed_ready_reassignment_stops_ready_until_fresh_command(self):
        row = offer(CHAT, minutes=10)
        member = person(CHAT)
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(CHAT, NOW + dt.timedelta(minutes=10), [row]), [])
            row["status"] = "cancelled"
            member["payload"].update(ready_at=(NOW + dt.timedelta(minutes=10, seconds=1)).isoformat())
            self.assertEqual(len(ready.available(CHAT, NOW + dt.timedelta(minutes=11), [row])), 1)

    def test_daily_board_excludes_yesterday_without_releasing_ownership(self):
        today, yesterday = offer(CHAT, accepted=True), offer(CHAT, accepted=True)
        today["payload"].update(new_client_contact_id="today", new_client_contact_name="Today client")
        yesterday["id"] = 99
        yesterday["payload"].update(new_client_contact_id="yesterday", new_client_contact_name="Yesterday client",
            new_client_acknowledged_at=(NOW - dt.timedelta(days=1)).isoformat())
        source = [(PAGES[0][0], [{"id": "yesterday", "name": "Yesterday client"}, {"id": "fresh", "name": "Fresh client"}])]
        original = copy.deepcopy(yesterday)
        with patch.object(workflow, "history_for", return_value=[yesterday, today]), patch.object(clients, "_page_contacts", return_value=source):
            snapshot = workflow.directory(CHAT, NOW + dt.timedelta(minutes=2))
        self.assertEqual({c["contact_id"] for c in snapshot["clients"]}, {"today", "fresh"})
        self.assertEqual(yesterday, original)
        text, _ = workflow.render_directory(snapshot)
        self.assertIn("Available clients", text)
        self.assertIn("11 · 1 accepted", text)
        self.assertNotIn("Yesterday client", text)
        self.assertEqual(snapshot["work_date"], "2026-10-22")

    def test_new_day_uses_new_board_and_stale_daily_board_cannot_deliver(self):
        with patch.object(workflow, "enqueue_scheduled_action") as queue:
            workflow.queue_board(CHAT, NOW)
            workflow.queue_board(CHAT, NOW + dt.timedelta(days=1))
        first, second = [c.kwargs for c in queue.call_args_list]
        self.assertNotEqual(first["dedupe_key"], second["dedupe_key"])
        self.assertFalse(workflow.prepare_delivery(first, NOW + dt.timedelta(days=1)))

    def test_acceptance_requests_daily_board_refresh(self):
        with patch.object(clients, "enqueue_scheduled_action"), patch.object(handoffs, "close_offers"), patch.object(workflow, "queue_board") as board:
            clients._queue_confirmation_ack(offer(CHAT, accepted=True), NOW)
        board.assert_called_once_with(CHAT, NOW, refresh=True)

    def test_long_daily_lists_include_every_client_without_navigation(self):
        entries = [{"client": f"Client{i:03d} " + "😀" * 100, "page": "<&>" * 100, "status": "Available", "available": True,
                    "contact_id": str(i), "editor_id": None} for i in range(50)]
        text, markup = workflow.render_directory({"clients": entries, "counts": {"Available": 50}, "daily_only": True,
                "work_date": "2026-10-22", "chat_id": CHAT, "team": "Veo"}, 1, own_view=True)
        import html, re
        for i in range(50):
            self.assertIn(f"Client{i:03d}", text)
        for part in workflow.directory_parts(text):
            self.assertLessEqual(len(html.unescape(re.sub(r"<[^>]*>", "", part)).encode("utf-16-le")) // 2, 4096)
        self.assertNotIn("Page 2/5", text)
        self.assertNotIn("Previous", str(markup))
        self.assertNotIn("Next", str(markup))
        self.assertIn("cw:view:0", str(markup))
