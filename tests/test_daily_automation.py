import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

from daily_automation import (
    GROUPS,
    build_group_report,
    parse_close_count,
    parse_page,
    parse_price,
    queue_daily_polls,
    quota_for,
)


class DailyAutomationTests(unittest.TestCase):
    def test_parses_current_deal_formats(self):
        text = "SEPTEMBER 18, 2026\nClient\nPD: 1,450\nPAGE: Manawari Studio"
        self.assertEqual(parse_price(text), Decimal("1450"))
        self.assertEqual(parse_page(text, GROUPS[-1003962888977]["pages"]), "Manawari Studios")
        self.assertEqual(parse_close_count("Page name: Azshinari\nClose Deal: 2"), 2)

    def test_quota_rounds_up(self):
        self.assertEqual(quota_for({"active_workers": 11}), (11, 9, True))
        self.assertEqual(quota_for(None), (0, 0, False))

    def test_report_calculates_commission_and_profit(self):
        rows = [
            {
                "thread_id": 1135,
                "text": "Price Deal: 1,000\nPage: Azshinari",
                "author_name": "Alex",
            },
            {
                "thread_id": 1135,
                "text": "Price Deal: 500\nPage: Sama kana media",
                "author_name": "Bea",
            },
            {
                "thread_id": 1132,
                "text": "Page name: Azshinari\nClose Deal: 2",
                "author_name": "Alex",
            },
            {
                "thread_id": 1132,
                "text": "Page name: Azshinari\nClose Deal: 1",
                "author_name": "Alex",
            },
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254,
                dt.date(2026, 9, 19),
                {"active_workers": 2},
            )
        self.assertIn("Quota: 2 DD and 2 CD", report)
        self.assertIn("Commission rate: 40%", report)
        self.assertIn("Gross price deals: ₱1,500", report)
        self.assertIn("Less employee commissions: ₱600", report)
        self.assertIn("Profit after commissions: ₱900", report)
        self.assertLess(report.index("Alex"), report.index("Bea"))

    def test_queues_nonanonymous_dated_polls_in_configured_topics(self):
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        with patch("daily_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            count = queue_daily_polls(dt.date(2026, 9, 20), now, set(GROUPS))
        self.assertEqual(count, 4)
        first = enqueue.call_args_list[0].kwargs
        self.assertFalse(first["payload"]["is_anonymous"])
        self.assertEqual(first["payload"]["daily_poll_date"], "2026-09-20")
        self.assertIn("September 20, 2026", first["payload"]["question"])

    def test_historical_report_classifies_exported_paid_and_close_entries(self):
        rows = [
            {
                "thread_id": None,
                "text": "Client\nPAID\nPrice Deal: 1,000\nTotal Payment: 1,000\nPage: Azshinari",
                "author_name": "Alex",
            },
            {
                "thread_id": None,
                "text": "Client\nDP: 150\nPrice Deal: 1,000\nPage: Azshinari",
                "author_name": "Alex",
            },
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254,
                dt.date(2026, 9, 18),
                {"active_workers": 1},
                historical=True,
            )
        self.assertIn("HISTORICAL SAMPLE", report)
        self.assertIn("Results: 1 DD | 1 CD", report)
        self.assertIn("Gross price deals: ₱1,000", report)
        self.assertIn("Reconstructed from the Telegram Desktop export", report)


if __name__ == "__main__":
    unittest.main()
