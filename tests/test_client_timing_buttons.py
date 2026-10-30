import datetime as dt
import html
import re
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import client_handoffs as handoffs
import client_volunteers as volunteers
import client_workflow as workflow
import new_client_automation as clients
import ready_availability as ready
from daily_automation import AVAILABILITY_GROUPS, TRABAWHO_CHAT_ID, GROUPS
from deal_parser import collect_deals
from test_client_workflow import assignment, callback, NOW, CHAT
from test_ready_handoffs import ready_offer


# Preserve optional paid-sale regression coverage; paused mode has its own tests.
def setUpModule():
    global _tracking_environment
    _tracking_environment = patch.dict("os.environ", {"DONE_DEAL_TRACKING_ENABLED": "true"})
    _tracking_environment.start()


def tearDownModule():
    _tracking_environment.stop()


class TimingAndButtonTests(unittest.TestCase):
    def test_new_offers_use_fifteen_minutes_in_every_configured_team(self):
        with patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"}):
            for chat in AVAILABILITY_GROUPS:
                self.assertEqual(clients._timeout_minutes(chat), 15)
                guide = workflow.guide(chat)
                self.assertIn("Accept within 15 minutes", guide)
                self.assertIn(f"{ready.ready_minutes(chat)} minutes", guide)

    def test_new_warning_starts_at_ten_minutes_and_ends_at_fifteen(self):
        row = ready_offer()
        row["payload"].update(new_client_response_minutes=15, _first_delivery_at=NOW.isoformat())
        with patch.object(handoffs, "enqueue_scheduled_action") as enqueue:
            handoffs.warning(row, NOW + dt.timedelta(minutes=9, seconds=59))
            enqueue.assert_not_called()
            handoffs.warning(row, NOW + dt.timedelta(minutes=10))
            self.assertIn("ends in 5 minutes", enqueue.call_args.kwargs["payload"]["text"])
            handoffs.warning(row, NOW + dt.timedelta(minutes=13))
            self.assertIn("ends in 2 minutes", enqueue.call_args.kwargs["payload"]["text"])
            handoffs.warning(row, NOW + dt.timedelta(minutes=15))
            self.assertEqual(enqueue.call_count, 2)

    def test_deferred_warning_recomputes_remaining_time_at_delivery(self):
        row = ready_offer()
        row["payload"].update(new_client_response_minutes=15, _first_delivery_at=NOW.isoformat())
        action = {"payload": {"client_handoff_parent_id": row["id"], "client_handoff_kind": "warning",
                              "text": "Your acceptance window ends in 5 minutes."}}
        with patch.object(handoffs, "request", return_value=[row]):
            self.assertTrue(handoffs.delivery_allowed(action, NOW + dt.timedelta(minutes=13)))
            self.assertIn("ends in 2 minutes", action["payload"]["text"])
            self.assertFalse(handoffs.delivery_allowed(action, NOW + dt.timedelta(minutes=15)))

    def test_original_five_minute_offer_keeps_original_warning(self):
        row = ready_offer()
        row["payload"]["_first_delivery_at"] = NOW.isoformat()
        with patch.object(handoffs, "enqueue_scheduled_action") as enqueue:
            handoffs.warning(row, NOW + dt.timedelta(minutes=3))
            self.assertIn("ends in 2 minutes", enqueue.call_args.kwargs["payload"]["text"])
            handoffs.warning(row, NOW + dt.timedelta(minutes=5))
            self.assertEqual(enqueue.call_count, 1)

    def test_member_pause_occurs_at_fifteen_minute_deadline(self):
        row = ready_offer()
        row["payload"].update(new_client_response_minutes=15, _first_delivery_at=NOW.isoformat())
        self.assertEqual(ready.missed_deadlines([row], NOW + dt.timedelta(minutes=14, seconds=59)), {})
        self.assertEqual(ready.missed_deadlines([row], NOW + dt.timedelta(minutes=15)), {11: NOW + dt.timedelta(minutes=15)})

    def test_acceptance_is_valid_before_fifteen_minutes_but_not_at_deadline(self):
        row = ready_offer()
        row["payload"].update(new_client_response_minutes=15, _first_delivery_at=NOW.isoformat())
        msg = {"from": {"id": 11}, "text": "WORKING " + row["payload"]["new_client_token"]}
        msg["date"] = int((NOW + dt.timedelta(minutes=14, seconds=59)).timestamp())
        self.assertTrue(volunteers.accepts(msg, row, 11))
        msg["date"] = int((NOW + dt.timedelta(minutes=15)).timestamp())
        self.assertFalse(volunteers.accepts(msg, row, 11))

    def test_missing_callback_topic_resolves_only_exact_delivered_message(self):
        incoming = callback("list:0")
        incoming["callback_query"]["message"].pop("message_thread_id")
        with patch.object(workflow, "request", return_value=[{"thread": str(GROUPS[CHAT]["contact_thread"])}]) as read, patch.object(workflow, "queue_message") as queue:
            self.assertIn("latest client list", workflow.handle(incoming, {CHAT}, NOW))
        params = parse_qs(urlsplit(read.call_args.args[0]).query)
        self.assertEqual(params["chat_id"], [f"eq.{CHAT}"])
        self.assertEqual(params["telegram_message_id"], ["eq.500"])
        self.assertEqual(queue.call_args.args[0], CHAT)

    def test_unknown_or_wrong_topic_missing_metadata_cannot_trigger_action(self):
        incoming = callback("ready")
        incoming["callback_query"]["message"].pop("message_thread_id")
        for rows in ([], [{"thread": "999"}], [{"thread": "invalid"}]):
            with patch.object(workflow, "request", return_value=rows), patch.object(ready, "handle") as handle:
                self.assertIsNone(workflow.handle(incoming, {CHAT}, NOW))
            handle.assert_not_called()

    def test_already_accepted_button_is_idempotent_for_current_owner(self):
        row = assignment()
        with patch.object(workflow, "history_for", return_value=[row]), patch.object(clients, "confirm_new_client_reply") as confirm:
            result = workflow.handle(callback("accept:AAAABBBB"), {CHAT}, NOW)
        self.assertIn("Already accepted", result)
        confirm.assert_not_called()

    def test_other_member_is_not_told_they_accepted_someone_elses_client(self):
        row = assignment()
        with patch.object(workflow, "history_for", return_value=[row]), patch.object(clients, "confirm_new_client_reply") as confirm:
            result = workflow.handle(callback("accept:AAAABBBB", actor=22), {CHAT}, NOW)
        self.assertIn("another editor", result)
        confirm.assert_not_called()

    def test_acceptance_race_does_not_report_success_without_committed_owner(self):
        row = assignment()
        row["payload"].pop("new_client_acknowledged_at")
        with patch.object(workflow, "history_for", return_value=[row]), patch.object(clients, "confirm_new_client_reply", return_value=True), patch.object(workflow, "queue_board") as board:
            result = workflow.handle(callback("accept:AAAABBBB"), {CHAT}, NOW)
        self.assertIn("offer changed", result)
        board.assert_not_called()

    def test_done_deal_draft_is_prefilled_owned_and_not_a_bot_sale(self):
        row = assignment(new_client_page="Vista Media Agency")
        with patch.object(workflow, "queue_message") as queue:
            result = workflow.done_deal_template(row, 11, NOW, "request-1", "650")
        self.assertIn("Template queued", result)
        text = queue.call_args.args[3]
        self.assertIn("not recorded yet", text)
        self.assertIn("Client: Client &amp; Name", text)
        self.assertIn("Page: Vista Media Agency", text)
        self.assertIn("Price Deal: 650", text)
        self.assertIn("Client token: AAAABBBB", text)
        self.assertIn(f"https://t.me/c/4423057797/{GROUPS[CHAT]['done']}", text)
        # It counts only after the employee posts the copied block in DONE DEALS.
        block = html.unescape(re.search(r"<pre>(.*?)</pre>", text, re.S)[1])
        bot_draft = {"message_id": 1001, "thread_id": GROUPS[CHAT]["contact_thread"], "text": block, "author_id": 999}
        self.assertEqual(collect_deals([bot_draft], GROUPS[CHAT])["entries"], [])
        employee_post = dict(bot_draft, thread_id=GROUPS[CHAT]["done"], author_id=11)
        entries = collect_deals([employee_post], GROUPS[CHAT])["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["gross"], 650)
        self.assertEqual(entries[0]["row"]["author_id"], 11)

    def test_done_deal_template_rejects_nonowner_and_invalid_amounts(self):
        row = assignment(new_client_page="Vista Media Agency")
        with patch.object(workflow, "queue_message") as queue:
            self.assertIn("Only the current editor", workflow.done_deal_template(row, 22, NOW, "bad", "650"))
            for value in ("-1", "650 + 50", "nan", "0", "1.001", "650\nPaid"):
                result = workflow.done_deal_template(row, 11, NOW, value, value)
                self.assertNotIn("Template queued", result)
        queue.assert_not_called()

    def test_suno_retains_receipts_without_veo_done_deal_button(self):
        row = assignment(TRABAWHO_CHAT_ID)
        buttons = workflow.owner_buttons(row)["inline_keyboard"]
        self.assertFalse(any("dd:" in b.get("callback_data", "") for line in buttons for b in line))
        with patch.object(workflow, "queue_message") as queue:
            self.assertIn("Suno receipts", workflow.done_deal_template(row, 11, NOW, "suno", "650"))
        queue.assert_not_called()

    def test_done_deal_text_fallback_and_button_both_prepare_templates(self):
        row = assignment(new_client_page="Vista Media Agency")
        message = callback("dd:100")
        with patch.object(workflow, "full_assignment", return_value=row), patch.object(workflow, "queue_message") as queue:
            self.assertIn("Template queued", workflow.handle(message, {CHAT}, NOW))
            self.assertIn("[ENTER AMOUNT]", queue.call_args.args[3])
        incoming = {"update_id": 1000, "message": {"chat": {"id": CHAT}, "from": {"id": 11},
                    "message_thread_id": GROUPS[CHAT]["contact_thread"], "text": "/ddtemplate AAAABBBB 650"}}
        with patch.object(workflow, "history_for", return_value=[row]), patch.object(workflow, "full_assignment", return_value=row), patch.object(workflow, "queue_message") as queue:
            self.assertIn("Template queued", workflow.handle(incoming, {CHAT}, NOW))
            self.assertIn("Price Deal: 650", queue.call_args.args[3])

    def test_done_deal_draft_is_suppressed_after_handover(self):
        row = assignment(new_client_assignee_id=22)
        action = {"chat_id": CHAT, "payload": {"client_template_assignment_id": 100, "client_template_owner_id": 11}}
        with patch.object(workflow, "full_assignment", return_value=row):
            self.assertFalse(workflow.prepare_delivery(action, NOW))


if __name__ == "__main__":
    unittest.main()
