import datetime as dt
import unittest
from unittest.mock import patch

import cloud_store
import member_capacity
import organized_reports
import page_capacity
import production_capacity as capacity
import staff_capacity
import tomorrow_capacity
from daily_automation import GROUPS

CHAT = -1003647732254
OTHER = -1004423057797
END = dt.date(2026, 10, 29)
START = END - dt.timedelta(days=55)
BASE = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)


def event(kind, client, hours, number, *, uid=11, chat=CHAT, price="699"):
    return {"chat_id": chat, "team": GROUPS[chat]["name"], "user_id": uid,
        "message_id": number, "sent_utc": (BASE + dt.timedelta(hours=hours)).isoformat(),
        "kind": kind, "client": client, "page": "Azshinari", "price": price, "source": "bot"}


def person(intervals=((0, 24), (12, 48), (48, 72)), *, uid=11):
    return {"user_id": uid, "name": "Member", "teams": [GROUPS[CHAT]["name"]],
        "project_history": [event(kind, f"client {i}", time, i * 2 + j, uid=uid)
            for i, (start, end) in enumerate(intervals)
            for j, (kind, time) in enumerate((("cd", start), ("dd", end)))],
        "daily": [{"date": str(BASE.date() + dt.timedelta(days=i)), "done_deals": count, "attendance": "Active"}
            for i, count in enumerate((0, 1, 1, 1, 0, 0))]}


def post(kind, client, hours, number, *, uid=11, name="Member", chat=CHAT):
    return {"message_id": number, "sent_utc": (BASE + dt.timedelta(hours=hours)).isoformat(),
        "author_id": uid, "author_name": name, "thread_id": GROUPS[chat]["close" if kind == "cd" else "done"],
        "text": f"Client: {client}\nDP: 150\nPrice Deal: 699\nPage: Azshinari" + ("\nPAID" if kind == "dd" else ""),
        "source": "bot", "content_type": "photo"}


class ProjectCapacityTests(unittest.TestCase):
    def test_cycle_concurrency_and_rate_use_elapsed_work_union(self):
        m = capacity.measure(person(), END)
        self.assertEqual(m["matched_projects"], 3)
        self.assertEqual(m["completion_dates"], 3)
        self.assertEqual(m["average_cd_to_dd_hours"], 28)
        self.assertEqual(m["median_cd_to_dd_hours"], 24)
        self.assertAlmostEqual(m["average_concurrent_projects"], 84 / 72)
        self.assertEqual(m["peak_concurrent_projects"], 2)
        self.assertEqual(m["matched_work_elapsed_hours"], 72)
        self.assertAlmostEqual(m["estimated_capacity_per_elapsed_day"], 1)
        self.assertIsNone(m["maximum_capacity"])

    def test_idle_gaps_do_not_lower_capacity_or_become_work_hours(self):
        continuous = capacity.measure(person(((0, 12), (24, 36), (48, 60))), END)
        sparse = capacity.measure(person(((0, 12), (72, 84), (144, 156))), END)
        self.assertEqual(continuous["estimated_capacity_per_elapsed_day"], 2)
        self.assertEqual(continuous["estimated_capacity_per_elapsed_day"], sparse["estimated_capacity_per_elapsed_day"])
        self.assertEqual(sparse["matched_work_elapsed_hours"], 36)

    def test_a_brief_peak_is_not_treated_as_sustained_concurrency(self):
        m = capacity.measure(person(((0, 24), (23.99, 48), (47.99, 72))), END)
        self.assertEqual(m["peak_concurrent_projects"], 2)
        self.assertLess(m["average_concurrent_projects"], 1.001)
        self.assertAlmostEqual(m["estimated_capacity_per_elapsed_day"], 1)

    def test_sparse_or_one_day_projects_do_not_establish_capacity(self):
        for intervals in (((0, 12), (24, 36)), ((0, 1), (0, 2), (0, 3))):
            with self.subTest(intervals=intervals):
                m = capacity.measure(person(intervals), END)
                self.assertIsNone(m["estimated_capacity_per_elapsed_day"])
                self.assertIsNotNone(m["average_cd_to_dd_hours"])

    def test_average_dd_includes_zero_active_days_but_keeps_output_day_mean_separate(self):
        p = person()
        p["daily"] += [{"date": "2026-10-28", "done_deals": 10, "attendance": "Unknown"},
                       {"date": "2026-10-29", "done_deals": 2, "attendance": "Inactive"}]
        m = capacity.measure(p, END)
        self.assertEqual(m["average_dd_per_active_day"], .5)
        self.assertEqual(m["active_dates"], 6)
        self.assertEqual(m["average_dd_per_output_day"], 3)

    def test_unknown_attendance_does_not_prevent_elapsed_project_measurement(self):
        p = person()
        p["daily"] = []
        m = capacity.measure(p, END)
        self.assertEqual(m["estimated_capacity_per_elapsed_day"], 1)
        self.assertIsNone(m["average_dd_per_active_day"])

    def test_censored_unmatched_and_ambiguous_records_cannot_inflate_concurrency(self):
        p = person()
        p["project_history"] += [event("cd", "unresolved", -50, 100), event("dd", "unmatched", 70, 101),
            event("cd", "ambiguous", 0, 102), event("cd", "ambiguous", 20, 103), event("dd", "ambiguous", 70, 104)]
        m = capacity.measure(p, END)
        self.assertEqual(m["estimated_capacity_per_elapsed_day"], 1)
        self.assertEqual(m["unresolved_cd_records"], 1)
        self.assertEqual(m["unmatched_dd_records"], 1)
        self.assertEqual(m["ambiguous_records"], 1)
        self.assertEqual(m["coverage"], "partial")

    def test_reversed_zero_naive_and_future_timestamps_do_not_create_valid_pairs(self):
        for start, finish in ((24, 0), (0, 0), (300, 324)):
            p = person(((start, finish),))
            self.assertEqual(capacity.measure(p, END)["matched_projects"], 0)
        p = person(((0, 24),))
        p["project_history"][0]["sent_utc"] = "2026-10-20T00:00:00"
        self.assertEqual(capacity.measure(p, END)["matched_projects"], 0)

    def test_other_owner_page_chat_and_changed_price_are_not_guessed(self):
        for field, changed in (("user_id", 22), ("page", "other"), ("chat_id", OTHER), ("price", "1499")):
            p = person(((0, 24),))
            p["project_history"][1][field] = changed
            self.assertEqual(capacity.measure(p, END)["matched_projects"], 0, field)

    def test_repeated_client_after_completion_forms_a_new_project(self):
        p = person(((0, 24), (48, 72), (96, 120)))
        for e in p["project_history"]:
            e["client"] = "repeat customer"
        self.assertEqual(capacity.measure(p, END)["matched_projects"], 3)

    def test_duplicate_records_and_cross_team_copies_never_double_count_work(self):
        p = person()
        p["project_history"] *= 2
        self.assertEqual(capacity.measure(p, END)["matched_projects"], 3)
        p = person()
        p["project_history"] += [{**e, "chat_id": OTHER} for e in p["project_history"]]
        m = capacity.measure(p, END)
        self.assertEqual(m["matched_projects"], 0)
        self.assertEqual(m["ambiguous_records"], 6)

    def test_only_recent_completions_count_but_earlier_cd_is_retained(self):
        p = person(((0, 24),))
        p["project_history"][0]["sent_utc"] = "2026-08-01T00:00:00+00:00"
        m = capacity.measure(p, END)
        self.assertEqual(m["matched_projects"], 1)
        self.assertGreater(m["average_cd_to_dd_hours"], 56 * 24)
        self.assertEqual(capacity.measure(person(), END + dt.timedelta(days=56))["matched_projects"], 0)


class ProjectHistoryIntegrationTests(unittest.TestCase):
    def test_cd_only_member_keeps_unresolved_history_and_no_invented_capacity(self):
        g = member_capacity.summarize(CHAT, [post("cd", "A", 0, 1)], [], START, END)
        m = g["members"][0]
        self.assertEqual(m["done_deals"], 0)
        self.assertEqual(m["project_capacity"]["unresolved_cd_records"], 1)
        self.assertIsNone(m["project_capacity"]["estimated_capacity_per_elapsed_day"])

    def test_history_uses_earliest_duplicate_and_keeps_dd_counts_unchanged(self):
        rows = [post("cd", "A", 0, 1), post("cd", "A", 1, 2), post("dd", "A", 24, 3), post("dd", "A", 25, 4)]
        g = member_capacity.summarize(CHAT, rows, [], START, END, historical=True)
        m = g["members"][0]
        self.assertEqual(m["done_deals"], 1)
        self.assertEqual(m["project_capacity"]["average_cd_to_dd_hours"], 24)
        pair = m["project_capacity"]["evidence"][0]
        self.assertEqual((pair["cd"]["message_id"], pair["dd"]["message_id"]), (1, 3))
        self.assertEqual(pair["dd"]["source"], "bot")

    def test_exact_repeat_annotation_is_supported_but_fuzzy_names_are_not(self):
        for end_name, expected in (("A (repeat client)", 1), ("a repeat client", 1), ("AA", 0)):
            g = member_capacity.summarize(CHAT, [post("cd", "A", 0, 1), post("dd", end_name, 24, 2)], [], START, END)
            self.assertEqual(g["members"][0]["project_capacity"]["matched_projects"], expected)

    def test_export_without_unique_identity_cannot_establish_capacity(self):
        rows = [post("cd", "A", 0, 1, uid=None), post("dd", "A", 24, 2, uid=None)]
        for row in rows:
            row.update(source="export", thread_id=None)
        g = member_capacity.summarize(CHAT, rows, [], START, END, historical=True)
        self.assertEqual(g["members"][0]["project_capacity"]["matched_projects"], 0)
        rows.append(post("dd", "B", 48, 3))
        g = member_capacity.summarize(CHAT, rows, [], START, END, historical=True)
        self.assertEqual(g["members"][0]["project_capacity"]["matched_projects"], 1)

    def test_cd_summaries_and_transfers_are_excluded(self):
        rows = [post("cd", "A", 0, 1), post("dd", "A", 24, 2, uid=22)]
        rows[0]["text"] += "\nCD: 1"
        g = member_capacity.summarize(CHAT, rows, [], START, END)
        self.assertTrue(all(m["project_capacity"]["matched_projects"] == 0 for m in g["members"]))

    def test_archive_reads_both_topics_and_cd_export_candidates(self):
        cd = {**post("cd", "A", 0, 1), "thread_id": None, "source": "export", "reply_to_message_id": GROUPS[CHAT]["close"]}
        with patch.object(cloud_store, "request", side_effect=[[], [], [], [], [cd], [cd], []]) as read:
            result = cloud_store.capacity_export_messages(CHAT, GROUPS[CHAT]["done"], BASE, BASE + dt.timedelta(days=1), close_thread=GROUPS[CHAT]["close"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["thread_id"], GROUPS[CHAT]["close"])
        self.assertEqual(len(read.call_args_list), 7)
        with patch.object(member_capacity, "activity_messages", return_value=[]) as live, \
                patch.object(member_capacity, "capacity_export_messages", return_value=[]) as exports, \
                patch.object(member_capacity, "poll_answers_for_range", return_value=[]):
            member_capacity.archive_report(CHAT, START, END)
        self.assertEqual(live.call_args.args[3], {GROUPS[CHAT]["done"], GROUPS[CHAT]["close"]})
        self.assertEqual(exports.call_args.kwargs["close_thread"], GROUPS[CHAT]["close"])

    def test_team_combination_recomputes_overlap_instead_of_adding_rates(self):
        first, second = person(((0, 24), (24, 48), (48, 72))), person(((0, 24), (24, 48), (48, 72)))
        for e in second["project_history"]:
            e.update(chat_id=OTHER, client="other " + e["client"])
        for p in (first, second):
            p.update(last_activity_date=str(END), posts_needing_review=0)
        reports = [{"chat_id": chat, "team": GROUPS[chat]["name"], "start_date": str(START), "end_date": str(END), "members": [p]}
            for chat, p in ((CHAT, first), (OTHER, second))]
        p = member_capacity.combined(reports)["members"][0]
        self.assertEqual(p["project_capacity"]["matched_projects"], 6)
        self.assertEqual(p["project_capacity"]["average_concurrent_projects"], 2)
        self.assertEqual(p["project_capacity"]["estimated_capacity_per_elapsed_day"], 2)

    def test_person_team_page_and_tomorrow_use_project_capacity_without_sales_default(self):
        p = person()
        p["planning"] = {"planning_average_per_day": .2, "safe_plan_per_day": .16}
        p["workload_capacity"] = staff_capacity.measure(p, END)
        summary = staff_capacity.aggregate([p, p, {"user_id": 22}])
        self.assertEqual(summary["project_capacity"]["known_members"], 1)
        self.assertEqual(summary["project_capacity"]["known_total"], 1)
        self.assertIsNone(summary["project_capacity"]["total"])
        self.assertIn("1/2 members", "\n".join(staff_capacity.render(summary)))
        self.assertIn("Project-based capacity estimate: 1.00", organized_reports.veo_capacity_text(CHAT, END, [p]))
        default = {"policy": "test", "business": "veo", "capacity_per_day": .2}
        p["planning"]["policy"] = "test"
        survey = {CHAT: {"pages": {"Azshinari": "a"}, "access": {11: {"a"}}, "answered": {11: {"a"}}}}
        capacities = {"members": {str(CHAT): [p]}, "businesses": {"veo": [p]}, "defaults": {"veo": default}}
        with patch.object(organized_reports.capacity_defaults, "apply", side_effect=lambda m, d: m):
            result = tomorrow_capacity.build(END + dt.timedelta(days=1), [p], {CHAT: [{"user_id": 11}]}, history_end=END, default_override=default)
            pages = page_capacity.build(END, {CHAT}, {CHAT: [{"user_id": 11}]}, capacities, survey)
        self.assertEqual(result["overall"]["workload_capacity"]["project_capacity"]["total"], 1)
        self.assertEqual(result["overall"]["recorded_output_total"], .2)
        self.assertEqual(pages["businesses"][0]["pages"][0]["workload_capacity"]["project_capacity"]["total"], 1)
        self.assertIn("Project capacity 1.00 DD/elapsed day", "\n".join(page_capacity.render(pages)))

    def test_missing_ads_context_does_not_hide_available_project_capacity(self):
        p = person()
        p["workload_capacity"] = {**staff_capacity.measure(p, END), "context_source_status": "unavailable"}
        text = "\n".join(staff_capacity.render(staff_capacity.aggregate([p])))
        self.assertIn("Project-based capacity estimate: 1.00", text)
        self.assertIn("manager-confirmed supplied-work output remains unmeasured", text)


if __name__ == "__main__":
    unittest.main()
