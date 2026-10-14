import copy
import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

import app
import daily_automation as daily
import done_deal_summary as summary
import done_deal_tracking as tracking
import client_workflow as workflow
from test_client_workflow import assignment
from test_wsgi import call_app

CHAT = -1004423057797
CONFIG = daily.GROUPS[CHAT]
NOW = dt.datetime(2026, 10, 14, 13, tzinfo=dt.timezone.utc)


def sale(message, amount="650", *, page="Vista Media Agency", client=None, author=11, thread=None):
    return {"message_id": message, "thread_id": CONFIG["done"] if thread is None else thread,
            "sent_utc": NOW.isoformat(), "author_id": author, "author_name": "Alex", "content_type": "text",
            "text": f"Client: {client or 'Client ' + str(message)}\nPage: {page}\nPrice Deal: {amount}"}


class ReadOnlyDoneDealTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {"DONE_DEAL_TRACKING_ENABLED": "false", "DONE_DEAL_SUMMARY_ENABLED": "true",
                         "INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": f"{CHAT},{daily.DAILY_REPORTS_CHAT_ID}"})
        env.start()
        self.addCleanup(env.stop)

    def test_default_mode_reads_posts_without_advanced_tracking(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(tracking.mode(), "read_only")
            self.assertFalse(tracking.enabled())
            self.assertTrue(tracking.summary_enabled())
        self.assertNotIn("cw:dd:", str(workflow.owner_buttons(assignment())))
        self.assertNotIn("/ddtemplate", workflow.guide(CHAT))
        self.assertIn("No client-token code", workflow.guide(CHAT))

    def test_simple_posts_need_no_token_and_produce_count_total_average(self):
        rows = [sale(1), sale(2, "1000", page="Loki Media")]
        original = copy.deepcopy(rows)
        result = summary.summarize(rows, CONFIG)
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["total"], Decimal("1650"))
        self.assertEqual(result["average"], Decimal("825.00"))
        self.assertEqual(result["pages"]["Vista"]["count"], 1)
        self.assertEqual(rows, original)
        self.assertIn("Average per done deal: ₱825", summary.render(result))

    def test_average_rounds_to_centavos(self):
        result = summary.summarize([sale(1, "1"), sale(2, "1"), sale(3, "2")], CONFIG)
        self.assertEqual(result["average"], Decimal("1.33"))

    def test_duplicate_sale_is_counted_once(self):
        result = summary.summarize([sale(1, client="Maria"), sale(2, client="Maria")], CONFIG)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["total"], Decimal("650"))
        self.assertEqual(result["duplicate_count"], 1)

    def test_cross_author_conflict_is_reviewed_without_guessing_owner_or_amount(self):
        result = summary.summarize([sale(1, client="Maria"), sale(2, client="Maria", author=22)], CONFIG)
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["review_count"], 2)
        self.assertIsNone(result["average"])

    def test_bad_amount_wrong_page_and_image_only_posts_are_excluded_from_average(self):
        photo = sale(4)
        photo.update(text="", content_type="photo")
        result = summary.summarize([sale(1), sale(2, "pending"), sale(3, page="Another team"), photo], CONFIG)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["average"], Decimal("650.00"))
        self.assertEqual(result["review_count"], 3)
        self.assertIn("excluded from totals and average", summary.render(result))

    def test_close_deals_and_unrelated_topics_do_not_become_done_deals(self):
        result = summary.summarize([sale(1, thread=CONFIG["close"]), sale(2, thread=CONFIG["contact_thread"])], CONFIG)
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["average"])
        self.assertIn("not available; no readable deals", summary.render(result))

    def test_capped_message_reads_show_partial_coverage(self):
        text = summary.render(summary.summarize([sale(1)], CONFIG, capped=True))
        self.assertIn("Partial coverage", text)
        self.assertIn("not full-day totals", text)
        self.assertIn("Average per done deal: ₱650 (readable posts only)", text)

    def test_daily_report_includes_summary_without_earnings_targets_or_member_codes(self):
        with patch.object(daily, "messages_for_day", return_value=([sale(1), sale(2, "1000")], False)) as read:
            text = daily.build_group_report(CHAT, NOW.date(), {"active_workers": 3})
        read.assert_called_once()
        self.assertIn("Done deals: 2", text)
        self.assertIn("Recorded deal amounts: ₱1,650", text)
        self.assertIn("Average per done deal: ₱825", text)
        for forbidden in ("35%", "40%", "/ddtemplate", " DD •", "commission rate"):
            self.assertNotIn(forbidden, text)

    def test_scheduler_queues_summary_only_in_daily_reports(self):
        with patch.object(daily, "existing_closeout_chats", return_value=set()), patch.object(daily, "daily_poll_counts", return_value={CHAT: {"active_workers": 2}}), patch.object(daily, "messages_for_day", return_value=([sale(1)], False)), patch.object(daily, "record_automation_marker"), patch.object(daily, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(daily.queue_daily_closeout(NOW.date(), NOW, {CHAT, daily.DAILY_REPORTS_CHAT_ID}), 2)
        report = next(call.kwargs for call in enqueue.call_args_list if call.kwargs["chat_id"] == daily.DAILY_REPORTS_CHAT_ID)
        self.assertIn("DONE DEAL SUMMARY", report["payload"]["text"])
        self.assertFalse(report["payload"]["done_deal_tracking"])
        self.assertNotIn("reply_markup", report["payload"])
        self.assertTrue(tracking.delivery_allowed({"dedupe_key": report["dedupe_key"], "payload": report["payload"]}))

    def test_authenticated_preview_reads_real_data_without_sending_or_writing(self):
        with patch.object(daily, "messages_for_day", return_value=([sale(1)], False)) as read, patch.object(daily, "enqueue_scheduled_action") as queue:
            status, result = call_app("/api/reports/daily-summary", headers={"Authorization": "Bearer test"}, query=f"group={CHAT}&date=2026-10-14")
        self.assertEqual(status, 200)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["groups"][0]["average_amount"], "650.00")
        read.assert_called_once_with(CHAT, NOW.date())
        queue.assert_not_called()

    def test_preview_requires_approved_daily_reports_and_valid_group(self):
        path = "/api/reports/daily-summary"
        self.assertEqual(call_app(path)[0], 401)
        headers = {"Authorization": "Bearer test"}
        self.assertEqual(call_app(path, headers=headers, query="date=invalid")[0], 400)
        self.assertEqual(call_app(path, headers=headers, query="group=-100123")[0], 404)
        self.assertEqual(call_app(path, method="POST", headers=headers)[0], 405)
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT)}):
            self.assertEqual(call_app(path, headers=headers)[0], 403)

    def test_summaries_cover_all_approved_veo_teams_only(self):
        allowed = set(daily.GROUPS) | {daily.DAILY_REPORTS_CHAT_ID, daily.TRABAWHO_CHAT_ID}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": ",".join(map(str, allowed))}), patch.object(daily, "messages_for_day", return_value=([], False)):
            status, result = call_app("/api/reports/daily-summary", headers={"Authorization": "Bearer test"})
        self.assertEqual(status, 200)
        self.assertEqual({row["chat_id"] for row in result["groups"]}, set(daily.GROUPS))
