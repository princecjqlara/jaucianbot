import datetime as dt
import unittest
from unittest.mock import patch

import client_workflow
import daily_automation as daily
import ready_availability as ready
import referral_incentives as referrals
from deal_parser import parse_page
from test_referral_incentives import ENV, PERSON, START

LYN = -1004364797420
OLLIE2 = -1004435878080
NOW = dt.datetime(2026, 10, 14, 18, tzinfo=dt.timezone.utc)
ALLOWED = set(daily.AVAILABILITY_GROUPS) | {referrals.RECRUITS_CHAT, referrals.SHARES_CHAT}


class ReferralRestorationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {**ENV, "DONE_DEAL_TRACKING_ENABLED": "false", "NEW_CLIENT_READY_ENABLED": "true"})
        env.start()
        self.addCleanup(env.stop)

    def test_shares_read_every_team_without_reenabling_sale_templates(self):
        def deals(chat, *args):
            config = daily.GROUPS[chat]
            return [{"message_id": 1, "thread_id": config["done"], "author_id": 11,
                     "sent_utc": (START + dt.timedelta(hours=1)).isoformat(),
                     "text": f"Client: Buyer {chat}\nPage: {next(iter(config['pages'].values()))}\nPrice Deal: 699"}]
        receipt = {"message_id": 1, "thread_id": daily.TRABAWHO["receipts"], "author_id": 11,
                   "sent_utc": (START + dt.timedelta(hours=1)).isoformat(),
                   "text": "Client: Suno Buyer\nPage: Hiraya\n699 (140)"}
        with patch.object(referrals, "read_records", side_effect=[[{"payload": PERSON}], []]), patch.object(referrals, "activity_messages", side_effect=deals) as read, patch.object(referrals, "receipt_messages", return_value=[receipt]), patch.object(referrals, "lock_credit") as lock:
            report = referrals.build_report(START.date(), START + dt.timedelta(hours=2), ALLOWED)
        self.assertEqual({call.args[0] for call in read.call_args_list}, set(daily.GROUPS))
        self.assertIn("₱244.65", report)  # Seven independent 699 sales at 5%.
        self.assertNotIn("on hold", report)
        lock.assert_called_once()
        for chat in (LYN, OLLIE2):
            self.assertNotIn("/ddtemplate", client_workflow.guide(chat))

    def test_status_reports_shares_running_even_when_advanced_controls_are_off(self):
        with patch.object(referrals, "read_records", return_value=[]), patch.object(referrals, "request", return_value=[]):
            status = referrals.status(ALLOWED, NOW)
        self.assertTrue(status["enabled"])
        self.assertFalse(status["share_calculations_paused"])

    def test_recovery_queues_previous_day_once_in_private_reporting_topic(self):
        with patch.object(referrals, "setup_complete", return_value=False), patch.object(referrals, "enqueue_scheduled_action", return_value=True) as queue, patch.object(referrals, "insert_record") as marker:
            self.assertEqual(referrals.queue_share_recovery(NOW, ALLOWED), 1)
        action = queue.call_args.kwargs
        self.assertEqual(action["chat_id"], referrals.SHARES_CHAT)
        self.assertEqual(action["payload"]["message_thread_id"], referrals.SHARES_THREAD)
        self.assertEqual(action["payload"]["referral_report_date"], "2026-10-14")
        self.assertEqual(action["scheduled_for"], NOW)
        marker.assert_called_once()
        with patch.object(referrals, "setup_complete", return_value=True), patch.object(referrals, "enqueue_scheduled_action") as queue:
            self.assertEqual(referrals.queue_share_recovery(NOW, ALLOWED), 0)
        queue.assert_not_called()

    def test_failed_recovery_queue_can_retry_without_changing_credit(self):
        with patch.object(referrals, "setup_complete", return_value=False), patch.object(referrals, "enqueue_scheduled_action", side_effect=RuntimeError("outbox unavailable")), patch.object(referrals, "insert_record") as marker, patch.object(referrals, "lock_credit") as lock:
            with self.assertRaises(RuntimeError):
                referrals.queue_share_recovery(NOW, ALLOWED)
        marker.assert_not_called()
        lock.assert_not_called()

    def test_recovery_does_not_publish_financials_without_reporting_approval(self):
        with patch.object(referrals, "enqueue_scheduled_action") as queue:
            self.assertEqual(referrals.queue_share_recovery(NOW, ALLOWED - {referrals.SHARES_CHAT}), 0)
        queue.assert_not_called()

    def test_new_team_and_lyn_use_exact_topics_and_disjoint_verified_pages(self):
        self.assertEqual({name: daily.GROUPS[OLLIE2][name] for name in ("general", "announcements", "active", "close", "done", "contact_thread", "freebie")},
                         {"general": 1, "announcements": 26, "active": 22, "close": 18, "done": 17, "contact_thread": 15, "freebie": 14})
        self.assertEqual(set(daily.GROUPS[LYN]["crm_pages"]), {"Habi Media Agency", "Salin Media Agency"})
        self.assertEqual(set(daily.GROUPS[OLLIE2]["crm_pages"]), {"AdsMoto Media Agency", "AiTayo"})
        self.assertFalse(set(daily.GROUPS[LYN]["crm_pages"].values()) & set(daily.GROUPS[OLLIE2]["crm_pages"].values()))
        for chat in (LYN, OLLIE2):
            self.assertFalse(daily.page_mapping_pending(daily.GROUPS[chat]))
            self.assertTrue(ready.enabled(chat))
            self.assertNotIn("PAGE SETUP PENDING", client_workflow.guide(chat))

    def test_simple_page_names_are_readable_without_tokens(self):
        for chat, text, expected in ((LYN, "Habi", "Habi Media Agency"), (LYN, "Salin", "Salin Media Agency"),
                                     (OLLIE2, "Ads Moto", "AdsMoto Media Agency"), (OLLIE2, "AI TAYO", "AiTayo")):
            with self.subTest(page=text):
                self.assertEqual(parse_page("Page: " + text, daily.GROUPS[chat]["pages"]), expected)
