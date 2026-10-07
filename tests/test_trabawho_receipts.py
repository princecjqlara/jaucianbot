import datetime as dt
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.parse import parse_qs

import app
from cloud_store import receipt_messages
from daily_automation import TRABAWHO_CHAT_ID
from receipt_parser import collect_receipts, parse_receipt
from trabawho_receipts import receipt_report
from worker_activity import worker_activity_report


DAY = dt.date(2026, 9, 18)
NOW = dt.datetime(2026, 9, 18, 4, tzinfo=dt.timezone.utc)


def row(message_id=1, text="HIRAYA\n699 (140)", **kwargs):
    return {"chat_id": TRABAWHO_CHAT_ID, "message_id": message_id,
            "sent_utc": NOW.isoformat(), "author_id": 7, "author_name": "Alex",
            "text": text, "source": "bot", "thread_id": 4, "content_type": "photo",
            **kwargs}


class ReceiptParsingTests(unittest.TestCase):
    def test_recorded_parentheses_and_labeled_tip_counted_once(self):
        report = receipt_report([row(text="HIRAYA\n699 (140)\n100 (50) tip")], DAY, DAY)
        self.assertEqual(report["totals"]["gross"], Decimal(799))
        self.assertEqual(report["totals"]["salary"], Decimal(190))
        self.assertEqual(report["totals"]["company_share"], Decimal(609))
        self.assertEqual(report["totals"]["labeled_tips"], Decimal(100))
        self.assertEqual(report["totals"]["non_tip_gross"], Decimal(699))

    def test_historical_unlabeled_half_share_not_assumed_tip(self):
        report = receipt_report([row(text="Hiraya\n400(80)\n200(100)")], DAY, DAY)
        self.assertEqual((report["totals"]["gross"], report["totals"]["salary"]), (600, 180))
        self.assertEqual(report["totals"]["labeled_tips"], 0)

    def test_video_revision_and_minus_one_preserve_written_salary(self):
        parsed = parse_receipt("HIRAYA\nVID 399(200)\n200 (40) dagdag\n800 (400) revission\n300(150) minus one")
        self.assertTrue(parsed["readable"])
        self.assertEqual(sum(line["salary"] for line in parsed["lines"]), 790)

    def test_tip_labels_before_after_and_on_separate_line(self):
        for text in ["Tip100(50)", "100(50)tip", "TIP\n100 (50)"]:
            with self.subTest(text=text):
                parsed = parse_receipt(text)
                self.assertTrue(parsed["readable"])
                self.assertEqual(parsed["lines"][0]["kind"], "tip")

    def test_only_explicit_tip_can_infer_half_share(self):
        for text in ["Tip 100", "100 tips", "PHP100 tip", "TIP\n100"]:
            parsed = parse_receipt(text)
            self.assertTrue(parsed["readable"])
            self.assertEqual(parsed["lines"][0]["salary"], 50)
            self.assertEqual(parsed["lines"][0]["salary_basis"], "50/50 rule")
        for text in ["100", "Video 100", "699\n140"]:
            self.assertFalse(parse_receipt(text)["readable"])

    def test_currency_commas_and_cents(self):
        parsed = parse_receipt("₱1,250.50 (PHP250.10)\nTip ₱100.01")
        self.assertTrue(parsed["readable"])
        self.assertEqual(parsed["lines"][0]["gross"], Decimal("1250.50"))
        self.assertEqual(parsed["lines"][1]["salary"], Decimal("50.01"))

    def test_malformed_reversed_arithmetic_and_conflicting_tip_held_for_review(self):
        for text in ["HIRAYA\n(399)80", "699(140)\n100", "500(100+50)",
                     "500+100(150)", "100(200)", "Tip100(60)", "0(0)",
                     "500(100)\nTotal 500(100)"]:
            with self.subTest(text=text):
                report = receipt_report([row(text=text)], DAY, DAY)
                self.assertEqual(report["totals"]["salary"], 0)
                self.assertEqual(len(report["review"]), 1)

    def test_export_reply_chain_and_explicit_other_topic(self):
        rows = [row(1, thread_id=None, reply_to_message_id=4, source="export"),
                row(2, text=None, thread_id=None, reply_to_message_id=1),
                row(3, thread_id=None, reply_to_message_id=2),
                row(4, thread_id=1),
                row(5, thread_id=16, reply_to_message_id=4)]
        parsed = collect_receipts(rows)
        self.assertEqual([e["row"]["message_id"] for e in parsed["entries"]], [1, 3])
        self.assertEqual(parsed["attachment_only_posts"], 1)
        self.assertFalse(parsed["unresolved_topic_rows"])

    def test_unknown_topic_and_reply_cycle_do_not_become_payroll(self):
        rows = [row(1, thread_id=None, reply_to_message_id=99),
                row(2, thread_id=None, reply_to_message_id=3),
                row(3, thread_id=None, reply_to_message_id=2)]
        parsed = collect_receipts(rows)
        self.assertFalse(parsed["entries"])
        self.assertEqual(len(parsed["unresolved_topic_rows"]), 3)

    def test_nonmonetary_parenthetical_note_does_not_discard_valid_receipt(self):
        report = receipt_report([row(text="makata ret client (revision)\n200(40)")], DAY, DAY)
        self.assertEqual(report["totals"]["salary"], 40)
        self.assertFalse(report["review"])

    def test_unpaid_reply_excludes_target_but_does_not_cancel_unrelated_receipt(self):
        rows = [row(1), row(2, text="pakitanggal nito. hindi pa paid yan", author_id=9,
                            reply_to_message_id=1), row(3)]
        report = receipt_report(rows, DAY, DAY)
        self.assertEqual(report["totals"]["gross"], 699)
        self.assertEqual(report["review"][0]["related_message_ids"], [2])

    def test_duplicate_message_ids_latest_edit_and_distinct_equal_receipts(self):
        rows = [row(1, source="export"), row(1, text="500 (100)", edited_utc="2026-09-18T05:00:00Z"), row(2)]
        report = receipt_report(rows, DAY, DAY)
        self.assertEqual(report["totals"]["gross"], 1199)
        self.assertEqual(report["totals"]["receipt_posts"], 2)

    def test_distinct_telegram_ids_with_same_name_and_idless_exports_stay_separate(self):
        rows = [row(1), row(2, author_id=8), row(3, author_id=None, source="export")]
        self.assertEqual(len(receipt_report(rows, DAY, DAY)["workers"]), 3)

    def test_pht_day_boundary_and_outside_period_ancestor_not_counted(self):
        rows = [row(1, sent_utc="2026-09-17T15:59:59Z"),
                row(2, sent_utc="2026-09-17T16:00:00Z", thread_id=None, reply_to_message_id=1),
                row(3, sent_utc="2026-09-18T16:00:00Z")]
        report = receipt_report(rows, DAY, DAY)
        self.assertEqual(report["totals"]["receipt_posts"], 1)
        self.assertEqual(report["workers"][0]["receipt_evidence"][0]["message_id"], 2)

    def test_other_group_excluded_and_coverage_not_zero_sales_assertion(self):
        report = receipt_report([row(chat_id=-123)], DAY, DAY)
        self.assertFalse(report["workers"])
        self.assertIsNone(report["coverage"]["latest_receipt_utc"])
        self.assertIn("does not establish zero sales", report["coverage_note"])


class ReceiptReadTests(unittest.TestCase):
    def test_paginated_read_and_older_ancestor_are_read_only(self):
        with patch("cloud_store.READ_PAGE_SIZE", 1), patch("cloud_store.request", side_effect=[
            [row(1, thread_id=None, reply_to_message_id=99)], [],
            [row(99, thread_id=None, reply_to_message_id=4, sent_utc="2026-08-01T00:00:00Z")],
        ]) as request:
            rows = receipt_messages(TRABAWHO_CHAT_ID, NOW, NOW + dt.timedelta(days=1))
        self.assertEqual([r["message_id"] for r in rows], [1, 99])
        self.assertTrue(all(len(call.args) == 1 and not call.kwargs for call in request.call_args_list))
        first = parse_qs(request.call_args_list[0].args[0].split("?", 1)[1])
        self.assertNotIn("thread_id", first)
        ancestor = parse_qs(request.call_args_list[2].args[0].split("?", 1)[1])
        self.assertEqual(ancestor["message_id"], ["in.(99)"])

    def test_missing_ancestor_not_retried_forever(self):
        with patch("cloud_store.request", side_effect=[
            [row(1, thread_id=None, reply_to_message_id=99)], [],
        ]) as request:
            rows = receipt_messages(TRABAWHO_CHAT_ID, NOW, NOW + dt.timedelta(days=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(request.call_count, 2)

    def test_manager_activity_routes_to_trabawho_without_veo_commission_or_poll_requirements(self):
        with patch("trabawho_receipts.receipt_messages", return_value=[row()]), patch(
            "worker_activity.activity_messages"
        ) as veo:
            report = worker_activity_report(TRABAWHO_CHAT_ID, 1, NOW)
        veo.assert_not_called()
        self.assertEqual(report["totals"]["salary"], 140)

    def test_authenticated_manager_api_and_group_allowlist(self):
        env = {"QUERY_STRING": f"group={TRABAWHO_CHAT_ID}&days=1"}
        with patch("app.authorized", return_value=False):
            statuses = []
            app.workers_activity_route(env, lambda s, h: statuses.append(s))
        self.assertTrue(statuses[0].startswith("401"))
        for allowed, expected in [(set(), "403"), ({TRABAWHO_CHAT_ID}, "200")]:
            with patch("app.authorized", return_value=True), patch("app.allowed_chat_ids", return_value=allowed), patch(
                "app.worker_activity_report", return_value=receipt_report([row()], DAY, DAY)
            ):
                statuses = []
                body = app.workers_activity_route(env, lambda s, h: statuses.append(s))
                self.assertTrue(statuses[0].startswith(expected))
                if expected == "200":
                    self.assertEqual(json.loads(b"".join(body))["activity"]["totals"]["salary"], "140")
