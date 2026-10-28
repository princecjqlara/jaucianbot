import copy
import datetime as dt
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import organized_reports
import page_capacity
import staff_capacity as capacity
import tomorrow_capacity
import workload_context as context
from daily_automation import GROUPS, DAILY_REPORTS_CHAT_ID
from postgres_archive import compile_request
from test_wsgi import call_app

CHAT = -1003647732254
OTHER = -1004423057797
END = dt.date(2026, 10, 28)
NOW = dt.datetime(2026, 10, 29, 2, tzinfo=dt.timezone.utc)


def person(values=(1, 2, 1, 0, 0), uid=11):
    return {"user_id": uid, "name": str(uid), "teams": [GROUPS[CHAT]["name"]],
        "daily": [{"date": str(END - dt.timedelta(days=len(values) - 1 - i)),
            "attendance": "Active", "done_deals": value} for i, value in enumerate(values)]}


def record(day, work="unknown", *, chat=CHAT, uid=11, ads="unknown", actual=True):
    return {"chat_id": chat, "work_date": day, "user_id": uid, "workload": work,
        "ads": ads, "actual": actual, "source": "manager_record"}


class WorkloadMeasurementsTests(unittest.TestCase):
    def test_zero_sales_without_work_are_not_evidence_of_low_capacity(self):
        original = person((1, 2, 1))
        before = capacity.measure(original, END)
        after = capacity.measure(person((1, 2, 1, 0, 0, 0)), END)
        self.assertEqual(before["selling_day_median"], 1)
        self.assertEqual(before["selling_day_median"], after["selling_day_median"])
        self.assertIsNone(after["supplied_work_output_per_day"])
        self.assertEqual(after["unknown_work_zero_days"], 3)
        self.assertIsNone(after["maximum_capacity"])

    def test_repeated_selling_days_avoid_a_single_peak_and_need_three_dates(self):
        self.assertEqual(capacity.measure(person((1, 1, 20)), END)["selling_day_median"], 1)
        self.assertIsNone(capacity.measure(person((0, 0, 20)), END)["selling_day_median"])
        self.assertIsNone(capacity.measure(person((0, 0, 0)), END)["supplied_work_output_per_day"])

    def test_confirmed_sufficient_work_includes_real_zero_output(self):
        p = person((2, 2, 0, 0, 0))
        records = [record(d["date"], "sufficient" if i < 3 else "none") for i, d in enumerate(p["daily"])]
        result = capacity.measure(p, END, records, [CHAT])
        self.assertEqual(result["supplied_work_days"], 3)
        self.assertAlmostEqual(result["supplied_work_output_per_day"], 4 / 3)
        self.assertEqual(result["no_work_days"], 2)
        self.assertEqual(result["unknown_work_zero_days"], 0)
        self.assertEqual(len(result["supplied_work_evidence"]), 3)

    def test_ads_status_and_team_work_cannot_invent_personal_sufficient_work(self):
        p = person((1, 1, 1, 0))
        for status in ("running", "paused"):
            records = [record(d["date"], "sufficient", uid=None, ads=status) for d in p["daily"]]
            result = capacity.measure(p, END, records, [CHAT])
            self.assertEqual(result["supplied_work_days"], 0)
            self.assertIsNone(result["supplied_work_output_per_day"])
            self.assertEqual(result["selling_day_median"], 1)
        self.assertEqual(result["ads_paused_days"], 4)
        self.assertEqual(result["unknown_work_zero_days"], 1)

    def test_future_unknown_attendance_and_plan_records_cannot_supply_history(self):
        p = person((2, 2, 2, 0))
        records = [record(d["date"], "sufficient", actual=False) for d in p["daily"]]
        self.assertEqual(capacity.measure(p, END, records, [CHAT])["supplied_work_days"], 0)
        for d in p["daily"]:
            d["attendance"] = "Unknown"
        p["daily"].append({"date": "2026-10-29", "attendance": "Active", "done_deals": 100})
        result = capacity.measure(p, END)
        self.assertIsNone(result["selling_day_median"])
        self.assertEqual(result["active_days"], 0)

    def test_context_is_scoped_to_member_team_and_all_combined_sources(self):
        p = person((0, 0, 0))
        records = [record(d["date"], "sufficient", uid=22) for d in p["daily"]]
        self.assertEqual(capacity.measure(p, END, records, [CHAT])["supplied_work_days"], 0)
        records = [record(d["date"], "none", uid=None) for d in p["daily"]]
        self.assertEqual(capacity.measure(p, END, records, [CHAT, OTHER])["no_work_days"], 0)
        records += [record(d["date"], "limited", chat=OTHER, uid=None) for d in p["daily"]]
        result = capacity.measure(p, END, records, [CHAT, OTHER])
        self.assertEqual(result["limited_work_days"], 3)
        self.assertEqual(result["no_work_days"], 0)
        records += [record(d["date"], "sufficient", chat=OTHER) for d in p["daily"]]
        self.assertEqual(capacity.measure(p, END, records, [CHAT, OTHER])["supplied_work_output_per_day"], 0)

    def test_partial_reference_never_becomes_a_full_roster_total_or_zero(self):
        p = {**person(), "workload_capacity": capacity.measure(person(), END)}
        summary = capacity.aggregate([p, p, {"user_id": 22}])
        self.assertEqual(summary["active_members"], 2)
        self.assertEqual(summary["selling_day"]["known_total"], 1)
        self.assertIsNone(summary["selling_day"]["total"])
        self.assertEqual(summary["selling_day"]["known_members"], 1)
        text = "\n".join(capacity.render(summary))
        self.assertIn("partial; supported members only", text)
        self.assertIn("unmeasured", text)
        self.assertIsNone(capacity.aggregate([], tracked=False)["selling_day"]["total"])
        self.assertEqual(capacity.aggregate([])["selling_day"]["total"], 0)

    def test_enrichment_keeps_original_sales_forecast_and_other_business(self):
        p = {**person(), "planning": {"planning_average_per_day": .2}}
        original = {"members": {str(CHAT): [p], "-999": ["suno"]},
            "businesses": {"veo": [p], "suno": ["suno"]}, "defaults": {"veo": {"kept": True}}}
        saved = copy.deepcopy(original)
        result = capacity.enrich(original, END)
        self.assertEqual(original, saved)
        self.assertEqual(result["businesses"]["suno"], ["suno"])
        self.assertEqual(result["members"][str(CHAT)][0]["planning"], p["planning"])
        self.assertEqual(result["members"][str(CHAT)][0]["workload_capacity"]["selling_day_median"], 1)


class WorkloadRecordsTests(unittest.TestCase):
    def payload(self, **changes):
        return {"chat_id": CHAT, "work_date": str(END), "ads": "paused", "workload": "unknown",
            "note": "Confirmed ads paused; backlog work is unknown", **changes}

    def test_write_is_cancelled_metadata_and_compiles_for_database_transport(self):
        with patch.object(context, "request", return_value=[{"id": 1}]) as db:
            result = context.save(self.payload(source_message_id=35610), {CHAT}, NOW)
        path, row = db.call_args.args
        self.assertEqual(row["status"], "cancelled")
        self.assertNotIn("text", row["payload"])
        self.assertEqual(row["payload"]["ads"], "paused")
        self.assertEqual(result["delivery"], "none; reporting context only")
        self.assertTrue(result["actual"])
        statement, values, _ = compile_request(path, row, **db.call_args.kwargs)
        self.assertIn("INSERT INTO", statement.as_string())

    def test_future_plan_stays_nonactual_and_needs_reconfirmation(self):
        with patch.object(context, "request", return_value=[{"id": 1}]):
            result = context.save(self.payload(work_date="2026-10-30"), {CHAT}, NOW)
        self.assertFalse(result["actual"])

    def test_invalid_or_ambiguous_sufficient_work_never_writes(self):
        cases = [self.payload(chat_id=-999), self.payload(chat_id=True), self.payload(user_id=True),
            self.payload(workload="sufficient"), self.payload(ads="no"), self.payload(note=""),
            self.payload(work_date="2027-01-01"), self.payload(source_message_id=-1)]
        with patch.object(context, "request") as db:
            for data in cases:
                with self.subTest(data=data), self.assertRaises(ValueError):
                    context.save(data, {CHAT}, NOW)
        db.assert_not_called()

    def test_read_uses_both_date_bounds_and_never_writes(self):
        with patch.object(context, "request", return_value=[]) as db:
            self.assertEqual(context.read({CHAT}, END, END), [])
        path = db.call_args.args[0]
        params = parse_qs(urlsplit(path).query)
        self.assertEqual(params["payload->>work_date"], [f"gte.{END}", f"lte.{END}"])
        self.assertEqual(params["status"], ["eq.cancelled"])
        self.assertEqual(len(db.call_args.args), 1)
        statement, values, _ = compile_request(path)
        self.assertIn(str(END), values)

    def test_http_requires_auth_scopes_groups_and_get_is_read_only(self):
        env = {"INSIGHTS_API_KEY": "key", "ALLOWED_CHAT_IDS": str(CHAT)}
        headers = {"Authorization": "Bearer key"}
        with patch.dict(os.environ, env, clear=True), patch.object(context, "read", return_value=[]) as read, \
                patch.object(context, "save", return_value={"ok": True}) as save:
            self.assertEqual(call_app("/api/reports/workload", query=f"group={CHAT}&date={END}")[0], 401)
            status, data = call_app("/api/reports/workload", query=f"group={CHAT}&date={END}", headers=headers)
            self.assertEqual(status, 200)
            self.assertTrue(data["read_only"])
            save.assert_not_called()
            self.assertEqual(call_app("/api/reports/workload", query=f"group={OTHER}&date={END}", headers=headers)[0], 403)
            self.assertEqual(call_app("/api/reports/workload", method="POST", body=json.dumps(self.payload()).encode(), headers=headers)[0], 200)
            self.assertEqual(call_app("/api/reports/workload", method="DELETE", headers=headers)[0], 405)


class WorkloadReportTests(unittest.TestCase):
    def test_team_total_includes_active_members_pending_page_access(self):
        a, b = person(uid=11), person(uid=22)
        for p in (a, b):
            p["workload_capacity"] = capacity.measure(p, END)
            p["planning"] = {"planning_average_per_day": .2, "safe_plan_per_day": .16}
        survey = {CHAT: {"pages": {"Azshinari": "a"}, "access": {11: {"a"}}, "answered": {11: {"a"}}}}
        text = organized_reports.staffing_text(CHAT, END, [], [{"user_id": 11}, {"user_id": 22}],
            [a, b], page_inputs=survey)
        self.assertIn("TEAM OUTLOOK", text)
        self.assertIn("Selling-day reference: 2.00 DD/day", text)
        self.assertIn("Azshinari: 1 active", text)
        self.assertIn("Page-access votes pending: 1", text)

    def test_workload_source_failure_keeps_capacity_unknown_and_sales_available(self):
        p = person()
        raw = {"date": str(END), "members": {str(CHAT): [p]}}
        with patch.object(organized_reports.member_presence, "filter_current", return_value=([p], {})), \
                patch.object(context, "read", side_effect=RuntimeError("unavailable")):
            result = organized_reports.current_capacities(raw, {CHAT}, NOW)
        measured = result["businesses"]["veo"][0]
        self.assertIsNotNone(measured["planning"]["planning_average_per_day"])
        self.assertIsNone(measured["workload_capacity"]["supplied_work_output_per_day"])
        self.assertEqual(measured["workload_capacity"]["context_source_status"], "unavailable")

    def test_page_and_tomorrow_preview_separate_capacity_reference_from_sales(self):
        p = person((1, 2, 1, 0, 0))
        default = {"business": "veo", "policy": "unmodelled", "capacity_per_day": .2}
        enriched = capacity.enrich({"members": {str(CHAT): [p]}, "businesses": {"veo": [p]},
            "defaults": {"veo": default}}, END)
        survey = {CHAT: {"pages": {"Azshinari": "a", "Sama Ka Na Media": "s"},
            "access": {11: {"a"}}, "answered": {11: {"a", "s"}}}}
        with patch.object(organized_reports.capacity_defaults, "apply", side_effect=lambda m, d: m):
            # Existing recorded-sales expectation can be low; capacity evidence is independent.
            member = enriched["businesses"]["veo"][0]
            member["planning"] = {"planning_average_per_day": .2, "safe_plan_per_day": .16}
            pages = page_capacity.build(END, {CHAT}, {CHAT: [{"user_id": 11}]}, enriched, survey)
        text = "\n".join(page_capacity.render(pages, detailed=True))
        self.assertIn("Selling-day reference: 1.00 DD/day", text)
        self.assertIn("Recent-demand sales forecast: Avg 0.20", text)
        self.assertIn("Capacity with sufficient work: unmeasured", text)
        self.assertEqual(pages["businesses"][0]["pages"][0]["workload_capacity"]["selling_day"]["total"], 1)
        self.assertEqual(pages["businesses"][0]["pages"][1]["workload_capacity"]["selling_day"]["total"], 0)
        result = tomorrow_capacity.build(END + dt.timedelta(days=1), [member],
            {CHAT: [{"user_id": 11}], OTHER: [{"user_id": 11}]}, history_end=END)
        self.assertEqual(result["overall"]["workload_capacity"]["active_members"], 1)
        self.assertEqual(result["overall"]["workload_capacity"]["selling_day"]["total"], 1)

    def test_untracked_rosters_and_unknown_members_cannot_invent_capacity(self):
        result = tomorrow_capacity.build(END + dt.timedelta(days=1), [], {CHAT: None}, history_end=END)
        self.assertIsNone(result["overall"]["workload_capacity"]["selling_day"]["total"])
        result = tomorrow_capacity.build(END + dt.timedelta(days=1), [], {CHAT: [{"user_id": 99}]}, history_end=END)
        self.assertIsNone(result["overall"]["workload_capacity"]["selling_day"]["total"])
        self.assertIsNone(result["overall"]["workload_capacity"]["supplied_work"]["total"])


if __name__ == "__main__":
    unittest.main()
