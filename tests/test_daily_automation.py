import datetime as dt
import json
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from daily_automation import (
    GROUPS,
    build_group_report,
    deal_progress_for_day,
    deal_totals_for_day,
    parse_close_count,
    parse_page,
    parse_price,
    queue_daily_polls,
    queue_daily_reminders,
    quota_reminder,
    quota_for,
    run_due_daily_automation,
    split_message,
)


class DailyAutomationTests(unittest.TestCase):
    def test_report_reminder_and_worker_data_share_corrected_totals(self):
        text = "October 5, 2026\nClient A\nPD: 650 + 550 (two videos) = 1,200\nPage: Manawari Studios"
        rows = [
            {"message_id": 1, "thread_id": 7, "author_id": 1, "author_name": "Alex", "text": text},
            {"message_id": 2, "thread_id": 7, "author_id": 1, "author_name": "Alex", "text": text},
            {"message_id": 3, "thread_id": 6, "author_id": 1, "author_name": "Alex",
             "text": "Client B\nDP: 100\nPD: -\nPage: Manawari Studios"},
            {"message_id": 4, "thread_id": 6, "author_id": 1, "author_name": "Alex",
             "text": "Close Deal: 1\nPage: Manawari Studios"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)), patch(
            "freebie_automation.freebie_report_lines", return_value=[]
        ), patch("new_client_automation.new_client_report_lines", return_value=[]):
            report = build_group_report(-1003962888977, dt.date(2026, 10, 5), {"active_workers": 1})
            progress = deal_progress_for_day(-1003962888977, dt.date(2026, 10, 5))
        self.assertIn("Actual: 1 DD ✅ • 1 CD ✅", report)
        self.assertIn("Gross: ₱1,200", report)
        self.assertIn("Commissions: −₱480", report)
        self.assertEqual((progress["dd_total"], progress["cd_total"]), (1, 1))
        self.assertEqual(progress["workers"][0]["gross"], Decimal("1200"))
        self.assertIn("Excluded 1 repeated", report)

    def test_unreadable_posts_do_not_claim_everyone_has_a_deal(self):
        with patch("daily_automation.messages_for_day", return_value=([
            {"thread_id": 1135, "author_id": 1, "author_name": "Alex", "text": "Page: Azshinari\nPD: pending"},
        ], False)):
            report = build_group_report(-1003647732254, dt.date(2026, 10, 5), {"active_workers": 1},
                                        active_users=[{"user_id": 1, "user_name": "Alex"}])
        self.assertNotIn("Everyone has at least one recorded", report)
        self.assertIn("Please review a possible deal post for: Alex", report)

    def test_missing_poll_does_not_guess_commission_tier(self):
        with patch("daily_automation.messages_for_day", return_value=([
            {"thread_id": 1135, "author_name": "Alex", "text": "Page: Azshinari\nPD: 650"},
        ], False)):
            report = build_group_report(-1003647732254, dt.date(2026, 10, 5), None)
        self.assertIn("Commission: needs a quick data review", report)

    def test_employee_commissions_round_before_totaling(self):
        rows = [{"thread_id": 1135, "author_id": i, "author_name": str(i),
                 "text": "Page: Azshinari\nPD: 0.10"} for i in (1, 2)]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(-1003647732254, dt.date(2026, 10, 5), {"active_workers": 5})
        self.assertEqual(report.count("Pay: ₱0.04"), 2)
        self.assertIn("Commissions: −₱0.08", report)
        self.assertIn("Net profit: ₱0.12", report)

    def test_parses_current_deal_formats(self):
        text = "SEPTEMBER 18, 2026\nClient\nPD: 1,450\nPAGE: Manawari Studio"
        self.assertEqual(parse_price(text), Decimal("1450"))
        self.assertEqual(parse_page(text, GROUPS[-1003962888977]["pages"]), "Manawari Studios")
        self.assertEqual(parse_close_count("Page name: Azshinari\nClose Deal: 2"), 2)
        self.assertIsNone(parse_page("Page: Unrelated Brand", GROUPS[-1003962888977]["pages"]))
        self.assertIsNone(parse_close_count("Page: Azshinari\nPrice Deal: 0"))

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
        self.assertIn("Target: 2 DD • 2 CD", report)
        self.assertIn("Commission: 40%", report)
        self.assertIn("Gross: ₱1,500", report)
        self.assertIn("Commissions: −₱600", report)
        self.assertIn("Net profit: ₱900", report)
        self.assertLess(report.index("Alex"), report.index("Bea"))

    def test_queues_nonanonymous_dated_polls_in_configured_topics(self):
        now = dt.datetime(2026, 9, 19, tzinfo=dt.timezone.utc)
        with patch("daily_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            count = queue_daily_polls(dt.date(2026, 9, 20), now, set(GROUPS))
        self.assertEqual(count, 4)
        first = enqueue.call_args_list[0].kwargs
        self.assertFalse(first["payload"]["is_anonymous"])
        self.assertEqual(first["payload"]["daily_poll_date"], "2026-09-20")
        self.assertIn("Sunday, September 20, 2026", first["payload"]["question"])

    def test_setup_day_poll_says_active_for_today(self):
        now = dt.datetime(2026, 9, 18, 18, 0, tzinfo=dt.timezone.utc)
        with patch("daily_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            queue_daily_polls(dt.date(2026, 9, 19), now, {-1004461399292})
        self.assertIn("active today", enqueue.call_args.kwargs["payload"]["question"].lower())

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
        self.assertIn("Actual: 1 DD ✅ • 1 CD ✅", report)
        self.assertIn("Gross: ₱1,000", report)
        self.assertIn("Reconstructed from the Telegram Desktop export", report)

    def test_lists_active_poll_voters_without_either_deal(self):
        rows = [
            {"thread_id": 1135, "author_id": 1, "author_name": "Alex Renamed",
             "text": "Price Deal: 1,000\nPage: Azshinari"},
            {"thread_id": 1132, "author_id": 2, "author_name": "Bea",
             "text": "Page: Azshinari\nClose Deal: 1"},
            {"thread_id": 1132, "author_id": 3, "author_name": "Casey",
             "text": "Page: Azshinari\nClose Deal: 0"},
            {"thread_id": 1135, "author_id": 4, "author_name": "Dani",
             "text": "Price Deal missing\nPage: Azshinari"},
            {"thread_id": 1135, "author_id": 99, "author_name": "Eli",
             "text": "Price Deal: 700\nPage: Azshinari"},
            {"thread_id": 1135, "author_id": 6, "author_name": "Fran", "text": "Thanks!"},
        ]
        users = [
            {"user_id": 1, "user_name": "Alex"},
            {"user_id": 2, "user_name": "Bea"},
            {"user_id": 3, "user_name": "Casey"},
            {"user_id": 4, "user_name": "Dani"},
            {"user_id": 5, "user_name": "Eli"},
            {"user_id": 6, "user_name": "Fran"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)), \
             patch("daily_automation.daily_poll_active_users", return_value=users) as fetch:
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 19),
                {"poll_id": "poll-1", "active_workers": 6},
            )
        fetch.assert_called_once_with("poll-1")
        section = report.split("ACTIVE TEAM MEMBERS WITH NO RECORDED CD OR DD\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("• Casey", section)
        self.assertIn("• Eli", section)
        self.assertIn("• Fran", section)
        self.assertNotIn("• Alex", section)
        self.assertNotIn("• Bea", section)
        self.assertNotIn("• Dani", section)
        self.assertIn("Please review a possible deal post for: Dani", section)

    def test_historical_names_match_exported_deals_and_summaries(self):
        rows = [
            {"thread_id": None, "author_name": "Alex", "author_id": None,
             "text": "Page: Azshinari\nClose Deal: 2"},
            {"thread_id": None, "author_name": "Bea", "author_id": None,
             "text": "PAID\nPrice Deal: 1,000\nPage: Azshinari"},
        ]
        users = [
            {"user_id": None, "user_name": "alex"},
            {"user_id": None, "user_name": "Bea"},
            {"user_id": None, "user_name": "Casey"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 18),
                {"active_workers": 3}, historical=True, active_users=users,
            )
        section = report.split("ACTIVE TEAM MEMBERS WITH NO RECORDED CD OR DD\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(section, "• Casey")
        self.assertIn("Actual: 1 DD", report)
        self.assertIn("0 CD", report)

    def test_does_not_name_people_when_daily_messages_are_capped(self):
        with patch("daily_automation.messages_for_day", return_value=([], True)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 19),
                {"active_workers": 1},
                active_users=[{"user_id": 1, "user_name": "Alex"}],
            )
        self.assertIn(
            "ACTIVE TEAM MEMBERS WITH NO RECORDED CD OR DD\n"
            "Names aren't listed because the day exceeded the safe message-review limit.",
            report,
        )
        self.assertNotIn("• Alex", report)

    def test_zero_active_responses_do_not_qualify_for_40_percent(self):
        rows = [{
            "thread_id": 1135, "author_name": "Alex",
            "text": "Page: Azshinari\nPrice Deal: 1,000",
        }]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 19), {"active_workers": 0},
                active_users=[],
            )
        self.assertIn("Target: not set", report)
        self.assertIn("Commission: 35%", report)
        self.assertNotIn("Commission: 40%", report)

    def test_ambiguous_deal_data_does_not_guess_pay_or_profit(self):
        rows = [
            {"thread_id": 1135, "author_name": "Alex",
             "text": "Page: Azshinari\nPrice Deal: 1,000"},
            {"thread_id": 1135, "author_name": "Casey",
             "text": "Page: Azshinari\nPrice Deal: 800"},
            {"thread_id": 1132, "author_name": "Bea",
             "text": "Page: Azshinari\nClose Deal: pending"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 19), {"active_workers": 2},
                active_users=[],
            )
        self.assertIn("Commission: needs a quick data review", report)
        self.assertIn("Pay: pending data review", report)
        self.assertIn("Net profit: pending data review", report)

    def test_review_does_not_block_35_percent_when_quota_cannot_be_reached(self):
        rows = [
            {"thread_id": 1135, "author_name": "Alex",
             "text": "Page: Azshinari\nPrice Deal: 1,000"},
            {"thread_id": 1135, "author_name": "Bea",
             "text": "Page: Azshinari\nPrice Deal: 800"},
            {"thread_id": 1135, "author_name": "Bea", "content_type": "photo", "text": ""},
            {"thread_id": 1132, "author_name": "Alex",
             "text": "Page: Azshinari\nClose Deal: 4"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 21), {"active_workers": 5},
                active_users=[],
            )
        self.assertIn("Target: 4 DD", report)
        self.assertIn("Commission: 35%", report)
        self.assertIn("Pay:", report)
        self.assertIn("Net profit:", report)
        self.assertIn("Please review 1 possible Done Deals post", report)
        self.assertNotIn("pending data review", report)

    def test_casual_topic_replies_are_not_flagged_as_broken_deals(self):
        rows = [
            {"thread_id": 1135, "author_name": "Alex", "text": "Thanks!"},
            {"thread_id": 1132, "author_name": "Bea", "text": "Great work"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            report = build_group_report(
                -1003647732254, dt.date(2026, 9, 19), {"active_workers": 1},
                active_users=[],
            )
        self.assertNotIn("DATA CHECK", report)

    def test_reminder_totals_follow_report_deal_rules(self):
        rows = [
            {"thread_id": 1135, "text": "Page: Azshinari\nPrice Deal: 1,000"},
            {"thread_id": 1135, "text": "Page: Azshinari\nPrice Deal: 500"},
            {"thread_id": 1132, "author_name": "Alex", "text": "Page: Azshinari\nClose Deal: 2"},
            {"thread_id": 1132, "author_name": "Alex", "text": "Page: Azshinari\nClose Deal: 1"},
            {"thread_id": 1132, "author_name": "Bea", "text": "Page: Azshinari\nPD: 900"},
            {"thread_id": 1135, "text": "Thanks"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            totals = deal_totals_for_day(-1003647732254, dt.date(2026, 9, 19))
        self.assertEqual(totals, (2, 3, False, False))
        reminder = quota_reminder(
            GROUPS[-1003647732254], dt.date(2026, 9, 19), 7,
            {"active_workers": 5}, *totals,
        )
        self.assertIn("Target: 4 DD and 4 CD", reminder)
        self.assertIn("So far: 2 DD • 3 CD", reminder)
        self.assertIn("Still needed: 2 DD • 1 CD", reminder)

    def test_smart_reminder_shows_leader_and_active_members_needing_help(self):
        rows = [
            {"thread_id": 1135, "author_id": 1, "author_name": "Alex",
             "text": "Page: Azshinari\nPrice Deal: 1,000"},
            {"thread_id": 1132, "author_id": 2, "author_name": "Bea",
             "text": "Page: Azshinari\nClose Deal: 2"},
            {"thread_id": 1135, "author_id": 3, "author_name": "Casey",
             "text": "Page: Azshinari\nPrice Deal: pending"},
        ]
        active_users = [
            {"user_id": 1, "user_name": "Alex"},
            {"user_id": 2, "user_name": "Bea"},
            {"user_id": 3, "user_name": "Casey"},
            {"user_id": 4, "user_name": "Dani"},
        ]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            progress = deal_progress_for_day(-1003647732254, dt.date(2026, 9, 19))
        reminder = quota_reminder(
            GROUPS[-1003647732254], dt.date(2026, 9, 19), 19,
            {"active_workers": 4},
            progress["dd_total"], progress["cd_total"], progress["capped"], progress["uncertain"],
            progress=progress, active_users=active_users,
        )
        self.assertIn("Current sales leader: Alex — 1 DD, ₱1,000 sales", reminder)
        self.assertIn("Active members with no recorded CD or DD yet: Dani.", reminder)
        self.assertIn("Possible activity awaiting a readable post: Casey.", reminder)
        self.assertNotIn("yet: Alex", reminder)
        self.assertNotIn("yet: Bea", reminder)

    def test_reminders_go_to_all_announcements_topics_once_per_slot(self):
        now = dt.datetime(2026, 9, 18, 23, tzinfo=dt.timezone.utc)
        with patch("daily_automation.daily_poll_counts", return_value={
            chat_id: {"active_workers": 5} for chat_id in GROUPS
        }), patch("daily_automation.existing_daily_reminder_chats", return_value=set()), \
             patch("daily_automation.deal_progress_for_day", return_value={
                 "dd_total": 2, "cd_total": 3, "capped": False, "uncertain": False,
                 "deal_rows": [], "uncertain_rows": [], "workers": [],
             }), \
             patch("daily_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            queued = queue_daily_reminders(dt.date(2026, 9, 19), 7, now, set(GROUPS))
        self.assertEqual(queued, 4)
        for call in enqueue.call_args_list:
            args = call.kwargs
            self.assertEqual(args["payload"]["message_thread_id"], GROUPS[args["chat_id"]]["announcements"])
            self.assertEqual(args["scheduled_for"], now)
            self.assertEqual(args["dedupe_key"], f"daily-reminder:2026-09-19:7:{args['chat_id']}")
            self.assertIn("Still needed: 2 DD • 1 CD", args["payload"]["text"])

    def test_existing_reminders_skip_progress_reads_and_duplicate_sends(self):
        date = dt.date(2026, 9, 19)
        now = dt.datetime(2026, 9, 18, 23, 30, tzinfo=dt.timezone.utc)
        with patch("daily_automation.existing_daily_reminder_chats", return_value=set(GROUPS)), \
             patch("daily_automation.daily_poll_counts") as counts, \
             patch("daily_automation.deal_progress_for_day") as deals, \
             patch("daily_automation.enqueue_scheduled_action") as enqueue:
            self.assertEqual(queue_daily_reminders(date, 7, now, set(GROUPS)), 0)
        counts.assert_not_called()
        deals.assert_not_called()
        enqueue.assert_not_called()

    def test_reminder_avoids_unverified_quota_or_negative_remaining(self):
        config = GROUPS[-1003647732254]
        date = dt.date(2026, 9, 19)
        no_poll = quota_reminder(config, date, 13, None, 2, 1, False, False)
        self.assertIn("Active poll hasn't been recorded", no_poll)
        self.assertNotIn("Still needed:", no_poll)
        capped = quota_reminder(config, date, 19, {"active_workers": 5}, 2, 1, True, False)
        self.assertIn("manual check", capped)
        self.assertNotIn("Still needed:", capped)
        met = quota_reminder(config, date, 19, {"active_workers": 5}, 6, 4, False, False)
        self.assertIn("Still needed: 0 DD • 0 CD", met)

    def test_reminder_flags_unreadable_deal_posts_before_claiming_a_gap(self):
        rows = [{"thread_id": 1135, "text": "Page: Azshinari\nPrice Deal missing"}]
        with patch("daily_automation.messages_for_day", return_value=(rows, False)):
            totals = deal_totals_for_day(-1003647732254, dt.date(2026, 9, 19))
        self.assertEqual(totals, (0, 0, False, True))
        reminder = quota_reminder(
            GROUPS[-1003647732254], dt.date(2026, 9, 19), 7,
            {"active_workers": 5}, *totals,
        )
        self.assertIn("exact remaining gap isn't shown", reminder)
        self.assertNotIn("Still needed:", reminder)

    def test_split_message_never_leaves_an_oversized_line(self):
        parts = split_message("A" * 8101, limit=4000)
        self.assertEqual("".join(parts), "A" * 8101)
        self.assertTrue(all(len(part) <= 4000 for part in parts))

    def test_dispatch_queues_six_philippine_time_reminder_slots(self):
        chat_id = -1003647732254
        with patch("daily_automation.daily_poll_counts", return_value={chat_id: {}}), \
             patch("daily_automation.queue_daily_reminders", return_value=1) as reminders:
            for hour in (7, 10, 13, 16, 19, 21):
                now = dt.datetime(2026, 9, 19, hour, tzinfo=dt.timezone(dt.timedelta(hours=8)))
                run_due_daily_automation(now, {chat_id})
                reminders.assert_called_with(dt.date(2026, 9, 19), hour, now, {chat_id})
            self.assertEqual(reminders.call_count, 6)
            run_due_daily_automation(dt.datetime(2026, 9, 18, 23, 59, tzinfo=dt.timezone.utc), {chat_id})
            self.assertEqual(reminders.call_count, 7)
            run_due_daily_automation(dt.datetime(2026, 9, 19, 0, tzinfo=dt.timezone.utc), {chat_id})
            self.assertEqual(reminders.call_count, 7)

    def test_vercel_cron_covers_midnight_and_daytime_hours(self):
        config = json.loads((Path(__file__).resolve().parents[1] / "vercel.json").read_text())
        self.assertEqual(
            {(job["path"], job["schedule"]) for job in config["crons"]},
            {("/api/cron/dispatch", f"0 {hour} * * *") for hour in (*range(14), 16, 23)}
            | {("/api/cron/dispatch", "59 15 * * *")},
        )


if __name__ == "__main__":
    unittest.main()
