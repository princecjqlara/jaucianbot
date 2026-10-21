import copy
import unittest
from unittest.mock import patch

import app
import client_workflow as workflow
import daily_automation as daily
import done_deal_tracking as tracking
import new_client_automation as clients
import referral_incentives as referrals
import worker_activity
from daily_report_plans import plan_lines
from test_client_workflow import assignment, callback, CHAT, NOW
from test_new_client_automation import contact


class PausedTrackingTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict("os.environ", {"DONE_DEAL_TRACKING_ENABLED": "false", "DONE_DEAL_SUMMARY_ENABLED": "false", "NEW_CLIENT_READY_ENABLED": "true"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_tracking_is_off_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(tracking.enabled())

    def test_every_team_keeps_owner_and_availability_buttons_without_sale_controls(self):
        for chat in daily.AVAILABILITY_GROUPS:
            with self.subTest(chat=chat):
                buttons = workflow.owner_buttons(assignment(chat))
                self.assertNotIn("cw:dd:", str(buttons))
                self.assertNotIn("Hand over", str(buttons))
                self.assertIn("Client list", str(buttons))
                self.assertIn("Ready", str(workflow.menu()))
                self.assertIn("Busy", str(workflow.menu()))
                self.assertEqual(clients._timeout_minutes(chat), 15)
                guide = workflow.guide(chat)
                self.assertNotIn("/ddtemplate", guide)
                self.assertIn("Accept within 15 minutes", guide)
                if chat in daily.GROUPS:
                    self.assertIn("tracking is paused", guide)
                else:
                    self.assertIn("SONG SENT", guide)

    def test_old_sale_buttons_and_text_do_not_read_assignments_or_create_drafts(self):
        text = {"message": {"message_id": 50, "chat": {"id": CHAT}, "from": {"id": 11},
                           "message_thread_id": daily.GROUPS[CHAT]["contact_thread"], "text": "/ddtemplate AAAABBBB 650"}}
        with patch.object(workflow, "full_assignment") as read, patch.object(workflow, "history_for") as history, patch.object(workflow, "queue_message") as queue:
            self.assertIn("tracking is paused", workflow.handle(callback("dd:100"), {CHAT}, NOW))
            queue.assert_not_called()
            self.assertIn("tracking is paused", workflow.handle(text, {CHAT}, NOW))
            self.assertEqual(queue.call_count, 1)
            self.assertNotIn("client_template_assignment_id", queue.call_args.kwargs)
            read.assert_not_called()
            history.assert_not_called()

    def test_pending_drafts_are_suppressed_before_any_assignment_read(self):
        action = {"chat_id": CHAT, "payload": {"client_template_assignment_id": 100}}
        with patch.object(workflow, "full_assignment") as read:
            self.assertFalse(workflow.prepare_delivery(action, NOW))
            self.assertFalse(tracking.delivery_allowed(action))
            read.assert_not_called()

    def test_legacy_quota_and_sales_reports_are_suppressed_but_new_outputs_pass(self):
        for key in ("daily-reminder:old", "daily-quota:old", "daily-report:old", "historical-report:old", "ready-instructions:v6:old", "ready-rollout:v6:old"):
            self.assertFalse(tracking.delivery_allowed({"dedupe_key": key, "payload": {"text": "old sales prompt"}}))
        self.assertTrue(tracking.delivery_allowed({"dedupe_key": "daily-report:new", "payload": {"done_deal_tracking": False}}))

    def test_legacy_client_offer_keeps_ownership_and_deadline_when_sale_footer_removed(self):
        payload = {"new_client_token": "AAAABBBB", "new_client_assignee_id": 11, "new_client_response_minutes": 15,
                   "text": "Accept within 15 minutes.\n\nWhen this client pays, include <code>Client token: AAAABBBB</code> in your Done Deal post, along with the page, client name and price, so we can link the sale to this assignment.",
                   "reply_markup": workflow.owner_buttons(assignment())}
        payload["reply_markup"]["inline_keyboard"].append([workflow.button("Old draft", "dd:100")])
        original = copy.deepcopy(payload)
        self.assertTrue(tracking.delivery_allowed({"chat_id": CHAT, "payload": payload}))
        self.assertNotIn("Done Deal", payload["text"])
        self.assertNotIn("cw:dd:", str(payload["reply_markup"]))
        for name in ("new_client_token", "new_client_assignee_id", "new_client_response_minutes"):
            self.assertEqual(payload[name], original[name])

    def test_new_veo_offers_have_no_sale_post_instruction(self):
        for chat, config in daily.GROUPS.items():
            page = next(iter(config["crm_pages"]), "Pending page")
            offer, reminder = clients._assignment_text({"user_id": 11, "user_name": "Alex"}, page, contact("client", NOW.isoformat()), "AAAABBBB", 1, chat_id=chat)
            for text in (offer, reminder):
                self.assertIn("WORKING AAAABBBB", text)
                self.assertNotIn("Done Deal", text)

    def test_close_deal_reports_exclude_paid_posts_and_financial_totals(self):
        config = daily.GROUPS[CHAT]
        rows = [{"message_id": 1, "author_id": 11, "author_name": "Alex", "sent_utc": NOW.isoformat(),
                 "thread_id": config["close"], "text": "Page: Vista Media Agency\nClose Deal: 2"},
                {"message_id": 2, "author_id": 11, "author_name": "Alex", "sent_utc": NOW.isoformat(),
                 "thread_id": config["done"], "text": "Page: Vista Media Agency\nPrice Deal: 650\nPaid"}]
        with patch.object(daily, "messages_for_day", return_value=(rows, False)):
            progress = daily.deal_progress_for_day(CHAT, NOW.date())
            self.assertEqual(progress["cd_total"], 2)
            self.assertEqual(progress["dd_total"], 0)
            report = daily.build_group_report(CHAT, NOW.date(), {"active_workers": 2})
        self.assertIn("Recorded close deals: 2", report)
        for value in ("40%", "35%", "₱650", "0 DD", "sales leader", "Price Deal"):
            self.assertNotIn(value, report)

    def test_new_plans_and_reminders_have_only_close_deal_targets(self):
        for chat, config in daily.GROUPS.items():
            count = {"active_workers": 5}
            for text in (daily.quota_announcement(config, NOW.date(), count),
                         daily.quota_reminder(config, NOW.date(), 10, count, 100, 2, False, False),
                         "\n".join(plan_lines(chat, NOW.date(), count))):
                self.assertNotIn(" DD", text)
                self.assertNotIn("40%", text)
                self.assertNotIn("35%", text)
                self.assertIn("8", text)

    def test_worker_report_marks_sales_unavailable_and_preserves_client_responses(self):
        with patch.object(worker_activity, "activity_messages", return_value=[]) as read, patch.object(worker_activity, "poll_answers_for_range", return_value=[]), patch.object(worker_activity, "freebie_actions", return_value=[]), patch.object(worker_activity, "new_client_actions", return_value=[assignment()]):
            report = worker_activity.worker_activity_report(CHAT, 1, NOW)
        self.assertEqual(read.call_args.args[3], {daily.GROUPS[CHAT]["close"]})
        self.assertEqual(report["done_deal_tracking"], "paused")
        self.assertIsNone(report["leaders"]["sales"])
        self.assertIsNone(report["leaders"]["client_conversion"])
        self.assertIn("client_response_minutes", report["leaders"])
        self.assertIn("unavailable, not zero", report["coverage_note"])
        for worker in report["workers"]:
            self.assertIsNone(worker["dd"])
            self.assertIsNone(worker["client_performance"]["conversion_percent"])

    def test_recruiter_shares_are_independent_of_advanced_done_deal_controls(self):
        with patch.dict("os.environ", {"REFERRAL_START_DATE": "2026-10-09", "REFERRAL_COMMISSION_BASIS": "gross", "REFERRAL_WINDOW_START": "join_or_vote"}), patch.object(referrals, "read_records", return_value=[]) as read, patch.object(referrals, "lock_credit") as lock:
            report = referrals.build_report(NOW.date(), NOW, referrals.WORK_TEAMS)
        self.assertIn("RECRUITER DAILY REPORT", report)
        self.assertNotIn("on hold", report)
        self.assertEqual(read.call_count, 2)
        lock.assert_not_called()

    def test_delivery_never_sends_old_paid_sale_output(self):
        action = {"id": 700, "chat_id": CHAT, "dedupe_key": "daily-report:old", "payload": {"text": "old earnings"}}
        with patch.object(app, "finish_scheduled_action", return_value=True) as finish, patch.object(app, "send_scheduled_action") as send:
            self.assertEqual(app._deliver_claimed_actions([action], {CHAT}), (1, 0, 0))
        finish.assert_called_once()
        send.assert_not_called()

    def test_suno_receipt_report_stays_separate(self):
        with patch("trabawho_receipts.trabawho_receipt_report", return_value={"receipts": "available"}) as receipts:
            self.assertEqual(worker_activity.worker_activity_report(daily.TRABAWHO_CHAT_ID, 1, NOW), {"receipts": "available"})
        receipts.assert_called_once_with(1, NOW)

    def test_ready_and_busy_callbacks_still_route_in_every_team(self):
        import ready_availability as ready
        for chat in daily.AVAILABILITY_GROUPS:
            for command in ("ready", "busy"):
                with self.subTest(chat=chat, command=command), patch.object(ready, "handle") as handle, patch("suno_store.suno_configured", return_value=True):
                    self.assertEqual(workflow.handle(callback(command, chat=chat), {chat}, NOW), "Availability updated.")
                    self.assertEqual(handle.call_args.args[0]["message"]["text"], "/" + command)

    def test_suno_handover_is_disabled_without_changing_owner_or_acceptance(self):
        def save(row, payload, now):
            row["payload"] = payload
            return True
        for chat in (daily.TRABAWHO_CHAT_ID,):
            row = assignment(chat)
            with self.subTest(chat=chat), patch.object(workflow, "is_member", return_value=True), patch.object(workflow, "save_assignment", side_effect=save), patch.object(workflow, "reconcile_row"):
                self.assertEqual(workflow.start_handover(row, 11, 22, "Bea", "Draft is ready", NOW, "request", 0), workflow.HANDOVER_DISABLED)
                self.assertEqual(row["payload"]["new_client_assignee_id"], 11)
                workflow.finish_handover(row, 22, 1, "yes", NOW, "accept")
                self.assertEqual(row["payload"]["new_client_assignee_id"], 11)
                self.assertEqual(row["payload"]["new_client_token"], "AAAABBBB")
                self.assertEqual(row["payload"]["new_client_acknowledged_at"], NOW.isoformat())
