import datetime as dt
import json
import unittest
from unittest.mock import patch

import app
import cloud_store
import member_capacity as capacity
from daily_automation import GROUPS, MANILA
from test_wsgi import call_app

CHAT = -1004423057797
START = dt.date(2026, 10, 19)
END = dt.date(2026, 10, 25)


def sale(day, number, *, uid=11, name="Melo"):
    return {"author_id": uid, "author_name": name, "message_id": day.day * 100 + number,
            "sent_utc": dt.datetime.combine(day, dt.time(2), tzinfo=dt.timezone.utc).isoformat(),
            "thread_id": GROUPS[CHAT]["done"], "source": "bot", "content_type": "text",
            "text": f"Client: Client {day}-{number}\nPage: Vista Media Agency\nPrice Deal: 500"}


def votes(pattern, **extra):
    return [{"user_id": 11, "user_name": "Melo", "active": active,
             "daily_polls": {"work_date": (START + dt.timedelta(days=i)).isoformat()}, **extra}
            for i, active in enumerate(pattern)]


def sales_by_day(counts):
    return [sale(START + dt.timedelta(days=i), n) for i, count in enumerate(counts) for n in range(count)]


class MemberCapacityTests(unittest.TestCase):
    def member(self, counts, answers, end=END):
        result = capacity.summarize(CHAT, sales_by_day(counts), answers, START, end)
        return result["members"][0]

    def test_supplied_melo_week_excludes_declared_inactive_days(self):
        m = self.member([3, 0, 1, 4, 0, 2, 5], votes([True, False, True, True, False, True, True]))
        self.assertEqual(m["done_deals"], 15)
        self.assertEqual(m["known_active_days"], 5)
        self.assertEqual(m["known_inactive_days"], 2)
        self.assertEqual(m["average_per_known_active_day"], 3)
        self.assertEqual(m["unknown_attendance_days"], 0)
        self.assertEqual(m["observed_daily_peak"], 5)
        self.assertIsNone(m["maximum_capacity"])

    def test_unknown_attendance_example_reports_output_mean_and_observed_peak(self):
        m = self.member([3, 0, 1], [], START + dt.timedelta(days=2))
        self.assertEqual(m["average_per_output_day"], 2)
        self.assertEqual(m["confirmed_output_days"], 2)
        self.assertEqual(m["observed_daily_peak"], 3)
        self.assertIsNone(m["average_per_known_active_day"])
        self.assertEqual(m["unknown_attendance_days"], 3)
        self.assertEqual(m["daily"][1]["attendance"], "Unknown")

    def test_active_days_with_zero_done_deals_stay_in_denominator(self):
        m = self.member([3, 0, 1], votes([True, True, True]), START + dt.timedelta(days=2))
        self.assertEqual(m["known_active_days"], 3)
        self.assertEqual(m["average_per_known_active_day"], 1.33)
        self.assertEqual(m["average_per_output_day"], 2)

    def test_partial_attendance_uses_matching_active_day_numerator(self):
        m = self.member([3, 0, 1], votes([True]), START + dt.timedelta(days=2))
        self.assertEqual(m["done_deals"], 4)
        self.assertEqual(m["done_deals_on_known_active_days"], 3)
        self.assertEqual(m["average_per_known_active_day"], 3)
        self.assertEqual(m["unknown_attendance_days"], 2)

    def test_output_on_inactive_day_is_reported_without_silently_changing_attendance(self):
        m = self.member([3, 0, 1], votes([True, False, False]), START + dt.timedelta(days=2))
        self.assertEqual(m["output_on_inactive_days"], 1)
        self.assertEqual(m["average_per_known_active_day"], 3)
        self.assertEqual(m["daily"][2]["attendance"], "Inactive")

    def test_availability_poll_does_not_prove_full_shift_or_maximum_capacity(self):
        m = self.member([3, 4, 5], votes([True, True, True]), START + dt.timedelta(days=2))
        self.assertEqual(m["verified_full_working_days"], 0)
        self.assertIsNone(m["top_three_full_day_average"])
        self.assertIsNone(m["maximum_capacity"])

    def test_three_explicit_full_days_allow_top_three_average(self):
        m = self.member([3, 0, 1, 4], votes([True] * 4, full_day_verified=True), START + dt.timedelta(days=3))
        self.assertEqual(m["top_three_full_day_average"], 2.67)
        self.assertIsNone(m["maximum_capacity"])

    def test_duplicates_unreadable_posts_and_evidence_keep_shared_safeguards(self):
        first = sale(START, 0)
        duplicate = {**first, "message_id": 999}
        unreadable = {**sale(START, 1), "text": "Page: Vista Media Agency\nPrice Deal: unreadable"}
        r = capacity.summarize(CHAT, [first, duplicate, unreadable], [], START, END)
        self.assertEqual(r["members"][0]["done_deals"], 1)
        self.assertEqual(r["duplicate_posts_excluded"], 1)
        self.assertEqual(r["posts_needing_review"], 1)
        evidence = r["evidence"][0]
        self.assertEqual(evidence["source"], "bot")
        self.assertEqual(evidence["team"], "Veo Ollie")
        self.assertEqual(evidence["message_id"], first["message_id"])
        self.assertIn("2026-10-19T02:00", evidence["sent_utc"])

    def test_midnight_boundary_uses_philippine_work_date(self):
        row = sale(START, 0)
        row["sent_utc"] = "2026-10-18T16:00:00+00:00"
        result = capacity.summarize(CHAT, [row], [], START, START)
        self.assertEqual(result["members"][0]["done_deals"], 1)
        self.assertEqual(result["evidence"][0]["work_date"], "2026-10-19")

    def test_zero_output_active_member_is_present_without_dividing_by_zero(self):
        result = capacity.summarize(CHAT, [], votes([True]), START, START)
        m = result["members"][0]
        self.assertEqual(m["done_deals"], 0)
        self.assertEqual(m["average_per_known_active_day"], 0)
        self.assertIsNone(m["average_per_output_day"])

    def test_ambiguous_names_with_distinct_user_ids_are_never_merged(self):
        rows = [sale(START, 0, uid=11), sale(START, 1, uid=22), sale(START, 2, uid=None)]
        result = capacity.summarize(CHAT, rows, [], START, START)
        self.assertEqual(len(result["members"]), 3)

    def test_report_reads_completed_days_and_not_partial_today(self):
        now = dt.datetime(2026, 10, 22, 1, tzinfo=dt.timezone.utc)
        with patch.object(capacity, "activity_messages", return_value=[]) as messages, \
                patch.object(capacity, "poll_answers_for_range", return_value=[]) as answers:
            r = capacity.report(CHAT, 3, now)
        self.assertEqual(r["start_date"], "2026-10-19")
        self.assertEqual(r["end_date"], "2026-10-21")
        self.assertEqual(messages.call_args.args[2], dt.datetime(2026, 10, 21, 16, tzinfo=dt.timezone.utc))
        self.assertEqual(answers.call_args.args[1:], (START, dt.date(2026, 10, 21)))

    def test_endpoint_is_read_only_authenticated_and_group_scoped(self):
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), \
                patch.object(capacity, "report", return_value={"members": [], "start_date": "2026-10-19", "end_date": "2026-10-21"}) as report, patch.object(cloud_store, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(call_app("/api/workers/capacity")[0], 401)
            self.assertEqual(call_app("/api/workers/capacity", headers={"Authorization": "Bearer test"}, query="group=-100123")[0], 404)
            status, body = call_app("/api/workers/capacity", headers={"Authorization": "Bearer test"}, query=f"group={CHAT}&days=3")
            self.assertEqual(status, 200)
            self.assertTrue(body["read_only"])
            enqueue.assert_not_called()
            self.assertEqual(report.call_args.args[:2], (CHAT, 3))

    def test_source_failure_is_unavailable_not_zero_output(self):
        with patch.dict("os.environ", {"INSIGHTS_API_KEY": "test", "ALLOWED_CHAT_IDS": str(CHAT)}, clear=True), \
                patch.object(capacity, "report", side_effect=RuntimeError("private source detail")):
            status, body = call_app("/api/workers/capacity", headers={"Authorization": "Bearer test"})
        self.assertEqual(status, 503)
        self.assertNotIn("private source detail", json.dumps(body))


    def test_cross_team_capacity_sums_daily_counts_and_counts_active_dates_once(self):
        first = capacity.summarize(CHAT, sales_by_day([3, 0, 1]), votes([True, True, True]), START, START + dt.timedelta(days=2))
        second = {**first, "team": "Other VEO team", "chat_id": -1003647732254,
                  "members": [{**first["members"][0], "daily": [
                      {"date": START.isoformat(), "done_deals": 2, "attendance": "Active"},
                      {"date": (START + dt.timedelta(days=1)).isoformat(), "done_deals": 1, "attendance": "Inactive"},
                      {"date": (START + dt.timedelta(days=2)).isoformat(), "done_deals": 0, "attendance": "Unknown"}]}]}
        m = capacity.combined([first, second])["members"][0]
        self.assertEqual(m["done_deals"], 7)
        self.assertEqual(m["known_active_days"], 3)
        self.assertEqual(m["average_per_known_active_day"], 2.33)
        self.assertEqual(m["confirmed_output_days"], 3)
        self.assertEqual(m["observed_daily_peak"], 5)
        self.assertIsNone(m["maximum_capacity"])

    def test_combined_missing_attendance_never_becomes_inactive_from_one_other_team(self):
        first = capacity.summarize(CHAT, [], votes([False]), START, START)
        second = {**first, "team": "Other team", "chat_id": -1003647732254,
                  "members": [{**first["members"][0], "daily": [{"date": START.isoformat(), "done_deals": 0, "attendance": "Unknown"}]}]}
        self.assertEqual(capacity.combined([first, second])["members"][0]["daily"][0]["attendance"], "Unknown")

    def test_same_display_name_without_ids_is_not_merged_across_teams(self):
        first = capacity.summarize(CHAT, [sale(START, 0, uid=None)], [], START, START)
        second = {**first, "chat_id": -1003647732254, "team": "Other team"}
        self.assertEqual(len(capacity.combined([first, second])["members"]), 2)


class CapacityPlanningTests(unittest.TestCase):
    def plan(self, counts, attendance=None):
        first = END - dt.timedelta(days=len(counts) - 1)
        daily = [{"date": (first + dt.timedelta(days=i)).isoformat(), "done_deals": n,
                  "attendance": attendance[i] if attendance else "Active"} for i, n in enumerate(counts)]
        return capacity.planning({"daily": daily}, END)

    def test_safe_plan_includes_zero_output_active_dates_and_rounds_down(self):
        p = self.plan([3, 0, 1, 4, 0, 2, 5, 0, 1, 2])
        self.assertEqual(p["active_day_average"], 1.8)
        self.assertEqual(p["known_active_days"], 10)
        self.assertEqual(p["safe_plan_per_day"], 1)
        self.assertEqual(p["reachable_target_per_day"], 3)
        self.assertEqual(p["reachable_support_days"], 3)
        self.assertTrue(p["estimates_only"])

    def test_missing_attendance_never_substitutes_output_day_mean_as_safe_capacity(self):
        p = self.plan([3, 0, 1, 4, 0, 2, 5, 0, 1, 2], ["Unknown"] * 10)
        self.assertIsNone(p["safe_plan_per_day"])
        self.assertIsNone(p["active_day_average"])
        self.assertEqual(p["output_day_average"], 2.57)
        self.assertEqual(p["reachable_target_per_day"], 3)

    def test_single_peak_cannot_raise_either_plan_above_repeated_level(self):
        p = self.plan([1] * 9 + [20])
        self.assertEqual(p["active_day_average"], 2.9)
        self.assertEqual(p["safe_plan_per_day"], 1)
        self.assertEqual(p["reachable_target_per_day"], 1)
        self.assertEqual(p["reachable_support_days"], 10)

    def test_need_seven_active_dates_and_three_output_dates_before_safe_plan(self):
        p = self.plan([3] * 6)
        self.assertIsNone(p["safe_plan_per_day"])
        self.assertEqual(p["reachable_target_per_day"], 3)
        p = self.plan([0] * 8 + [10, 20])
        self.assertIsNone(p["safe_plan_per_day"])
        self.assertIsNone(p["reachable_target_per_day"])

    def test_average_below_one_is_retained_without_forcing_a_one_client_minimum(self):
        p = self.plan([0] * 7 + [1] * 3)
        self.assertEqual(p["active_day_average"], 0.3)
        self.assertEqual(p["safe_plan_per_day"], 0)
        self.assertEqual(p["reachable_target_per_day"], 1)

    def test_recent_completed_dates_exclude_old_peak_and_partial_future_day(self):
        first = END - dt.timedelta(days=29)
        days = [{"date": (first + dt.timedelta(days=i)).isoformat(), "done_deals": 1,
                 "attendance": "Active"} for i in range(30)]
        days += [{"date": (first - dt.timedelta(days=1)).isoformat(), "done_deals": 100, "attendance": "Active"},
                 {"date": (END + dt.timedelta(days=1)).isoformat(), "done_deals": 200, "attendance": "Active"}]
        p = capacity.planning({"daily": days}, END)
        self.assertEqual(p["known_active_days"], 30)
        self.assertEqual(p["safe_plan_per_day"], 1)
        self.assertEqual(p["reachable_target_per_day"], 1)
        self.assertEqual(p["window_start"], first.isoformat())

    def test_cross_team_output_is_combined_before_repeat_days_are_counted(self):
        first = capacity.summarize(CHAT, sales_by_day([1, 1, 0]), [], START, START + dt.timedelta(days=2))
        second = {**first, "team": "Other team", "chat_id": -1003647732254}
        m = capacity.combined([first, second])["members"][0]
        self.assertEqual(m["confirmed_output_days"], 2)
        self.assertIsNone(m["planning"]["reachable_target_per_day"])
        self.assertIsNone(m["maximum_capacity"])
