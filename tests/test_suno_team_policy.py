"""Current Suno parity with the expiring-ready client workflow."""
import copy
import datetime as dt
import html
import re
import unittest
from unittest.mock import patch

import app
import client_handoffs as handoffs
import client_volunteers as volunteers
import client_workflow as workflow
import daily_automation as daily
import hourly_availability as hourly
import new_client_automation as clients
import page_access as access
import ready_availability as ready
import suno_store
from test_veo_ready_policy import NOW, offer, person

CHAT = daily.TRABAWHO_CHAT_ID
PID = "suno:cxgynadprukyeuqbchbs:hiraya"


def suno_offer(**kwargs):
    row = offer(ready.OLLIE, **kwargs)
    row["chat_id"] = CHAT
    row["payload"].update(new_client_thread_id=7673, new_client_contact_page_id=PID)
    return row


class SunoTeamPolicyTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true", "NEW_CLIENT_VOLUNTEER_ENABLED": "true",
                                       "HOURLY_AVAILABILITY_START_DATE": "2026-10-09"})
        env.start()
        self.addCleanup(env.stop)

    def test_tomorrow_uses_one_public_yes_no_poll_and_old_hourly_votes_still_have_history(self):
        with patch.object(daily, "enqueue_scheduled_action", return_value=True) as send, \
                patch.object(hourly, "enqueue_scheduled_action") as hours:
            self.assertEqual(daily.queue_daily_polls(NOW.date(), NOW, {CHAT}), 1)
        p = send.call_args.kwargs["payload"]
        self.assertEqual(p["options"], ["Yes", "No"])
        self.assertEqual(p["message_thread_id"], 7581)
        self.assertFalse(p["allows_multiple_answers"])
        hours.assert_not_called()
        self.assertTrue(hourly.enabled_for_chat(CHAT, dt.date(2026, 10, 21)))
        self.assertFalse(hourly.enabled_for_chat(CHAT, dt.date(2026, 10, 22)))

    def test_continuous_legacy_ready_expires_and_hourly_votes_cannot_join_current_queue(self):
        member = person(CHAT)
        member["payload"].update(continuous=True, ready_until=(NOW + dt.timedelta(days=2)).isoformat())
        with patch.object(ready, "records", return_value=[member]), patch.object(hourly, "current_votes") as votes:
            self.assertEqual(len(ready.available(CHAT, NOW + dt.timedelta(minutes=59), [])), 1)
            self.assertEqual(ready.available(CHAT, NOW + dt.timedelta(minutes=60), []), [])
            votes.assert_not_called()

    def test_acceptance_consumes_ready_and_only_a_later_command_reopens_it(self):
        row = suno_offer(accepted=True)
        member = person(CHAT, state="reserved")
        member["payload"]["reservation_key"] = "old-offer"
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(CHAT, NOW + dt.timedelta(minutes=2), [row]), [])
            member["payload"].update(state="ready", ready_at=(NOW + dt.timedelta(minutes=2)).isoformat())
            self.assertEqual(len(ready.available(CHAT, NOW + dt.timedelta(minutes=2), [row])), 1)

    def test_missed_ready_client_pauses_member_until_new_command(self):
        row = suno_offer(minutes=10)
        member = person(CHAT)
        with patch.object(ready, "records", return_value=[member]):
            self.assertEqual(ready.available(CHAT, NOW + dt.timedelta(minutes=10), [row]), [])
            member["payload"]["ready_at"] = (NOW + dt.timedelta(minutes=11)).isoformat()
            row["status"] = "cancelled"
            self.assertEqual(len(ready.available(CHAT, NOW + dt.timedelta(minutes=11), [row])), 1)

    def test_reassignment_uses_earliest_ready_with_page_access_and_ten_minutes(self):
        previous = suno_offer()
        previous["status"] = "cancelled"
        previous["payload"].update(new_client_phase="expired", new_client_cancelled_reason=volunteers.EXPIRED)
        members = [{"user_id": uid, "user_name": str(uid), "updated_at": (NOW + dt.timedelta(minutes=i)).isoformat(),
                    "ready_record": person(CHAT, uid)} for i, uid in enumerate((22, 33))]
        contacts = [("Hiraya", [{"id": "missed", "name": "Client & Name", "page_id": PID,
                                  "collected_details": {"request": "song"}}])]
        with patch.object(clients, "new_client_actions", return_value=[previous]), \
                patch.object(clients, "new_client_action_matches_source", return_value=True), \
                patch.object(clients, "_reconcile_archived_confirmations"), patch.object(clients, "_release_stale_assignments"), \
                patch.object(handoffs, "resume_missed_offers"), patch.object(handoffs, "ended", return_value=True), \
                patch.object(ready, "queue_instructions", return_value=0), patch.object(ready, "available", return_value=members), \
                patch.object(ready, "reserve", return_value=True), patch.object(access, "members", return_value={22: {PID}, 33: {PID}}), \
                patch.object(clients, "_page_contacts", return_value=contacts), \
                patch.object(clients, "enqueue_scheduled_action", return_value=True) as send:
            self.assertEqual(clients.queue_new_client_assignments(NOW + dt.timedelta(minutes=20), {CHAT}), 1)
        p = send.call_args.kwargs["payload"]
        self.assertEqual(p["new_client_assignee_id"], 22)
        self.assertEqual(p["new_client_response_minutes"], 10)
        self.assertNotIn("TAKE", p["text"])

    def test_ten_source_pages_get_two_polls_and_withdrawal_only_clears_its_part(self):
        pages = {f"Page {i}": f"suno:project:{i}" for i in range(10)}
        with patch.object(access, "page_map", return_value=pages), \
                patch.object(access, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(access.queue_poll(CHAT, NOW), 2)
        actions = [c.kwargs for c in queue.call_args_list]
        self.assertEqual([len(a["payload"]["options"]) for a in actions], [10, 2])
        self.assertEqual({pid for a in actions for pid in a["payload"]["page_access_ids"]}, set(pages.values()))
        self.assertEqual(len({a["dedupe_key"] for a in actions}), 2)
        rows = [{"payload": {"user_id": 11, "page_ids": []}},
                {"payload": {"user_id": 11, "page_ids": [pages["Page 9"]], "page_access_part": 1}}]
        with patch.object(access, "page_map", return_value=pages), patch.object(access, "request", return_value=rows):
            for row in rows:
                row["payload"]["page_access_version"] = access.version(CHAT)
            self.assertEqual(access.members(CHAT), {11: {pages["Page 9"]}})
        meta = {"chat_id": CHAT, "payload": {"page_access_version": "v", "page_ids": [pages["Page 9"]], "page_access_part": 1}}
        update = {"update_id": 7, "poll_answer": {"poll_id": "p2", "option_ids": [], "user": {"id": 11}}}
        with patch.object(access, "context", return_value=meta), patch.object(access, "version", return_value="v"), \
                patch.object(access, "request", return_value=[{"id": 1}]) as store:
            self.assertTrue(access.save_answer(update, {CHAT}))
        self.assertTrue(store.call_args.args[1]["dedupe_key"].endswith(":part:1"))
        self.assertEqual(store.call_args.args[1]["payload"]["page_ids"], [])

    def test_second_page_poll_registration_does_not_suppress_it_as_first_part_retry(self):
        action = {"id": 2, "chat_id": CHAT, "action_type": "poll", "payload": {
            "page_access_version": "v", "page_access_part": 1, "page_access_ids": [PID]}}
        with patch.object(access, "version", return_value="v"), patch.object(access, "poll_record", return_value=None) as lookup, \
                patch.object(access, "register_poll", return_value=True), patch.object(app, "save_update"), \
                patch.object(app, "finish_scheduled_action", return_value=True), \
                patch.object(app, "send_scheduled_action", return_value={"message_id": 88, "poll": {"id": "p2"}}) as send:
            self.assertEqual(app._deliver_claimed_actions([action], {CHAT}), (1, 1, 0))
        lookup.assert_called_once_with(CHAT, 1)
        send.assert_called_once()

    def test_suno_page_ids_use_suno_source_and_never_veo_crm(self):
        with patch.dict("os.environ", {"SUNO_SUPABASE_URL": "https://example.supabase.co"}), \
                patch.object(suno_store, "suno_configured", return_value=True), \
                patch.object(suno_store, "suno_request", return_value=[{"id": "a", "name": "Hiraya"}]):
            self.assertEqual(suno_store.suno_pages(), {"Hiraya": "suno:example:a"})

    def test_handover_commands_and_old_pending_notices_are_disabled(self):
        from test_client_workflow import assignment, callback
        row = assignment(CHAT)
        self.assertNotIn("Hand over", str(workflow.owner_buttons(row)))
        self.assertIn("Client details", str(workflow.owner_buttons(row)))
        with patch.object(suno_store, "suno_configured", return_value=True), patch.object(workflow, "full_assignment") as read:
            self.assertEqual(workflow.handle(callback("choose:100:0:0", chat=CHAT), {CHAT}, NOW), workflow.HANDOVER_DISABLED)
            read.assert_not_called()
        self.assertFalse(workflow.prepare_delivery({"chat_id": CHAT,
            "payload": {"client_transfer_assignment_id": 100}}, NOW))

    def test_partial_client_information_survives_acceptance_and_fits_telegram(self):
        contact = {"id": "a", "name": "Client", "collected_details": {"Song": "Business jingle"},
                   "details_complete": False, "details_percent": 50, "missing_details": ["Deadline <date>"]}
        text, _ = clients._assignment_text({"user_id": 11, "user_name": "Alex"}, "Hiraya", contact,
                                           "AABBCCDD", 1, chat_id=CHAT)
        self.assertIn("Still to confirm: Deadline &lt;date&gt;", text)
        self.assertIn("Business jingle", text)
        self.assertIn("within 24 hours", text)
        row = suno_offer(accepted=True)
        row["payload"].update(new_client_collected_details={"Song": "Business jingle"},
                              new_client_missing_details=["Deadline <date>"])
        with patch.object(clients, "new_client_actions", return_value=[row]):
            action = {"chat_id": CHAT, "payload": {"new_client_ack_assignment_id": row["id"]}}
            self.assertTrue(clients.new_client_ack_delivery_allowed(action))
        self.assertIn("Client details", str(action["payload"]["reply_markup"]))
        with patch.object(volunteers, "notices", return_value=[]), patch.object(handoffs, "request", return_value=[]), \
                patch.object(handoffs, "enqueue_scheduled_action", return_value=True) as edits:
            handoffs.close_offers(row, NOW, "Accepted")
        self.assertIn("Business jingle", edits.call_args.kwargs["payload"]["text"])
        self.assertIn("Deadline &lt;date&gt;", edits.call_args.kwargs["payload"]["text"])
        contact["collected_details"] = {"request": "😀<&>" * 1000}
        text, _ = clients._assignment_text({"user_id": 11, "user_name": "Alex"}, "Hiraya", contact,
                                           "AABBCCDD", 1, chat_id=CHAT)
        visible = html.unescape(re.sub(r"<[^>]*>", "", text))
        self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4000)

    def test_daily_list_hides_yesterday_without_releasing_accepted_ownership(self):
        from test_client_workflow import assignment
        old = assignment(CHAT, new_client_acknowledged_at=(NOW - dt.timedelta(days=1)).isoformat())
        old["payload"].update(new_client_contact_page_id=PID)
        today = copy.deepcopy(old)
        today.update(id=101)
        today["payload"].update(new_client_contact_id="today", new_client_acknowledged_at=NOW.isoformat())
        with patch.object(workflow, "history_for", return_value=[old, today]), patch.object(clients, "_page_contacts", return_value=[]):
            board = workflow.directory(CHAT, NOW)
        self.assertTrue(board["daily_only"])
        self.assertEqual([c["contact_id"] for c in board["clients"]], ["today"])
        self.assertTrue(clients._reserved_contact_keys([old]))
