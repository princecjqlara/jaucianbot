import copy
import datetime as dt
import os
import unittest
from unittest.mock import patch

import capacity_defaults
import daily_automation as daily
import organized_reports as reports
import page_capacity as pages

TODAY = dt.date(2026, 10, 22)
NOW = dt.datetime(2026, 10, 22, 7, tzinfo=dt.timezone.utc)
VEO = -1003647732254
OTHER = -1004423057797
SUNO = daily.TRABAWHO_CHAT_ID
MAKATA = daily.MAKATA_CHAT_ID


def member(uid, rate):
    return {"user_id": uid, "name": str(uid), "planning": {
        "estimated_capacity_per_day": rate, "planning_average_per_day": rate,
        "safe_plan_per_day": int(rate * 0.8)}}


def inputs(mapping, access, answered=None):
    return {"pages": mapping, "access": access,
        "answered": answered if answered is not None else {uid: set(mapping.values()) for uid in access}}


def fixture_inputs():
    mappings = {VEO: {"Sama Ka Na Media": "main"}, OTHER: {"Vista Media Agency": "vista"},
        SUNO: {"Hiraya Studio": "suno"}, MAKATA: {"Makata Studios": "makata"}}
    return {chat: inputs(mapping, {uid: set(mapping.values()) for uid in (1, 2, 7)})
        for chat, mapping in mappings.items()}


class PageCapacityTests(unittest.TestCase):
    def capacities(self):
        return {"date": "2026-10-21", "members": {str(VEO): [member(1, 3), member(2, 1)]},
            "defaults": {"veo": capacity_defaults.baseline("veo", [])}}

    def test_each_page_averages_only_its_active_access_voters(self):
        survey = {VEO: inputs({"Page A": "a", "Page B": "b"}, {1: {"a"}, 2: {"b"}, 3: {"a"}})}
        data = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 1}, {"user_id": 2}]}, self.capacities(), survey)
        a, b = data["businesses"][0]["pages"]
        self.assertEqual((a["active"], a["average"], a["total"]), (1, 3, 3))
        self.assertEqual((b["active"], b["average"], b["total"]), (1, 1, 1))
        self.assertNotEqual(a["average"], 2)

    def test_access_without_tomorrow_availability_does_not_count(self):
        survey = {VEO: inputs({"Page A": "a"}, {1: {"a"}})}
        data = pages.build(TODAY, {VEO}, {VEO: []}, self.capacities(), survey)
        self.assertEqual(data["businesses"][0]["pages"][0]["active"], 0)
        self.assertIsNone(data["businesses"][0]["pages"][0]["average"])

    def test_same_page_and_member_in_multiple_groups_count_once(self):
        survey = {chat: inputs({"Shared page": "a"}, {1: {"a"}}) for chat in (VEO, OTHER)}
        caps = self.capacities()
        caps["members"][str(OTHER)] = [member(1, 3)]
        rosters = {chat: [{"user_id": 1}, {"user_id": 1}] for chat in (VEO, OTHER)}
        result = pages.build(TODAY, set(rosters), rosters, caps, survey)["businesses"][0]["pages"]
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["active"], result[0]["total"]), (1, 3))

    def test_multi_page_member_is_available_to_each_page_without_inventing_an_allocation(self):
        survey = {VEO: inputs({"A": "a", "B": "b"}, {1: {"a", "b"}})}
        data = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 1}]}, self.capacities(), survey)
        self.assertEqual([p["average"] for p in data["businesses"][0]["pages"]], [3, 3])
        self.assertIn("page totals overlap", "\n".join(pages.render(data)))

    def test_page_access_in_another_group_cannot_turn_a_no_vote_into_availability(self):
        survey = {VEO: inputs({"A": "a"}, {}), OTHER: inputs({"B": "b"}, {1: {"b"}})}
        caps = self.capacities()
        result = pages.build(TODAY, {VEO, OTHER}, {VEO: [{"user_id": 1}], OTHER: []}, caps, survey)
        self.assertEqual([p["active"] for p in result["businesses"][0]["pages"]], [0, 0])

    def test_missing_access_is_pending_even_for_a_single_page_group(self):
        survey = {VEO: inputs({"A": "a"}, {})}
        data = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 1}]}, self.capacities(), survey)
        text = "\n".join(pages.render(data))
        self.assertIn("A: 0 active · Avg pending access", text)
        self.assertIn("Page-access votes pending: 1", text)

    def test_explicit_no_access_is_not_treated_as_a_missing_vote(self):
        survey = {VEO: inputs({"A": "a"}, {1: set()})}
        result = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 1}]}, self.capacities(), survey)
        page = result["businesses"][0]["pages"][0]
        self.assertEqual(page["missing_access"], 0)
        self.assertEqual(page["active"], 0)
        self.assertNotIn("pending", "\n".join(pages.render(result)))

    def test_missing_availability_is_unknown_and_distinct_from_zero_active(self):
        survey = {VEO: inputs({"A": "a"}, {1: {"a"}})}
        result = pages.build(TODAY, {VEO}, {VEO: None}, self.capacities(), survey)
        self.assertIsNone(result["businesses"][0]["pages"][0]["active"])
        self.assertIn("A: availability pending", "\n".join(pages.render(result)))

    def test_missing_group_poll_marks_shared_page_partial(self):
        survey = {chat: inputs({"A": "a"}, {1: {"a"}}) for chat in (VEO, OTHER)}
        result = pages.build(TODAY, {VEO, OTHER}, {VEO: [{"user_id": 1}], OTHER: None}, self.capacities(), survey)
        self.assertEqual(result["businesses"][0]["pages"][0]["average"], 3)
        self.assertIn("(partial)", "\n".join(pages.render(result)))

    def test_new_member_default_is_used_only_after_both_votes(self):
        survey = {VEO: inputs({"A": "a", "B": "b"}, {7: {"a"}})}
        result = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 7}]}, self.capacities(), survey)
        a, b = result["businesses"][0]["pages"]
        self.assertEqual((a["average"], a["provisional"], a["conservative"]), (0.5, 1, 0.4))
        self.assertEqual(b["active"], 0)

    def test_unknown_capacity_does_not_become_an_average_for_all_active_members(self):
        caps = {"members": {str(SUNO): [member(1, 2)]}, "defaults": {"suno": capacity_defaults.baseline("suno", [])}}
        survey = {SUNO: inputs({"A": "a"}, {1: {"a"}, 2: {"a"}})}
        result = pages.build(TODAY, {SUNO}, {SUNO: [{"user_id": 1}, {"user_id": 2}]}, caps, survey)
        page = result["businesses"][0]["pages"][0]
        self.assertEqual(page["active"], 2)
        self.assertEqual(page["unknown"], 1)
        self.assertIsNone(page["average"])
        self.assertIn("Avg Unknown", "\n".join(pages.render(result)))

    def test_veo_and_suno_page_ids_and_units_stay_separate(self):
        survey = {VEO: inputs({"A": "a"}, {1: {"a"}}), SUNO: inputs({"A": "a"}, {1: {"a"}})}
        caps = self.capacities()
        caps["members"][str(SUNO)] = [member(1, 2)]
        result = pages.build(TODAY, {VEO, SUNO}, {VEO: [{"user_id": 1}], SUNO: [{"user_id": 1}]}, caps, survey)
        self.assertEqual([b["unit"] for b in result["businesses"]], ["DD/day", "receipts/day"])
        self.assertEqual([b["pages"][0]["average"] for b in result["businesses"]], [3, 2])

    def test_own_page_report_can_use_personal_history_from_another_veo_group(self):
        caps = self.capacities()
        caps["members"][str(VEO)] = []
        caps["businesses"] = {"veo": [member(1, 3)]}
        survey = {VEO: inputs({"A": "a"}, {1: {"a"}})}
        result = pages.build(TODAY, {VEO}, {VEO: [{"user_id": 1}]}, caps, survey)
        self.assertEqual(result["businesses"][0]["pages"][0]["average"], 3)

    def test_full_page_list_is_not_paginated_or_truncated(self):
        survey = {VEO: inputs({f"Page {i:03d}": str(i) for i in range(70)}, {})}
        result = pages.build(TODAY, {VEO}, {VEO: []}, self.capacities(), survey)
        text = "\n".join(pages.render(result))
        self.assertEqual(text.count(": 0 active"), 70)
        self.assertIn("Page 069", text)


class PageAccessLoadingTests(unittest.TestCase):
    def test_load_uses_answers_while_veo_new_client_assignment_is_off(self):
        answers = [{"user_id": 1, "page_ids": ["a"], "answered": True},
            {"user_id": 2, "page_ids": [], "answered": True},
            {"user_id": 3, "page_ids": [], "answered": False},
            {"user_id": 4, "page_ids": ["obsolete"]}]
        with patch.dict(os.environ, {"VEO_NEW_CLIENTS_ENABLED": "false"}), \
                patch.object(pages.page_access, "page_map", return_value={"A": "a"}), \
                patch.object(pages.page_access, "answer_records", return_value=answers):
            data = pages.load({VEO})[VEO]
        self.assertEqual(data["access"][1], {"a"})
        self.assertEqual(data["answered"][2], {"a"})
        self.assertNotIn(3, data["answered"])
        self.assertEqual(data["access"][4], set())

    def test_each_poll_part_confirms_only_its_own_pages(self):
        mapping = {f"Page {n}": str(n) for n in range(11)}
        with patch.object(pages.page_access, "page_map", return_value=mapping), \
                patch.object(pages.page_access, "answer_records", return_value=[
                    {"user_id": 1, "page_ids": ["10"], "page_access_part": 1, "answered": True}]):
            data = pages.load({VEO})[VEO]
        self.assertEqual(data["answered"][1], {"9", "10"})
        self.assertEqual(data["access"][1], {"10"})

    def test_page_source_failure_is_not_silently_replaced_with_a_group_average(self):
        with patch.object(pages.page_access, "page_map", side_effect=RuntimeError("unavailable")):
            with self.assertRaises(RuntimeError):
                pages.load({VEO})


class PageReportDeliveryTests(unittest.TestCase):
    def test_team_topic_uses_the_same_page_average_across_source_groups(self):
        survey = {chat: inputs({"Shared page": "shared"}, {1: {"shared"}, 2: {"shared"}})
            for chat in (VEO, OTHER)}
        caps = {"members": {str(VEO): [], str(OTHER): []},
            "businesses": {"veo": [member(1, 3), member(2, 1)]}, "page_inputs": survey}
        rosters = {VEO: [{"user_id": 1}], OTHER: [{"user_id": 2}]}
        item = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "payload": {
            "organized_staffing_date": str(TODAY), "organized_staffing_version": reports.VERSION,
            "report_source_ids": [VEO, OTHER], "report_source_chat_id": VEO}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, OTHER, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(caps, rosters, rosters)):
            self.assertTrue(reports.prepare_delivery(item, NOW))
        self.assertIn("Shared page: 2 active · Avg 2.00", item["payload"]["text"])
        self.assertNotIn("Avg 3.00", item["payload"]["text"])

    def test_existing_daily_report_posts_edit_when_page_access_changes(self):
        survey = {VEO: inputs({"A": "a", "B": "b"}, {1: {"a"}})}
        caps = {"members": {str(VEO): [member(1, 3)]}, "page_inputs": survey}
        rosters = {VEO: [{"user_id": 1}]}
        item = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "telegram_message_id": 252, "repeat_interval_minutes": 5,
            "payload": {"organized_staffing_date": str(TODAY), "organized_staffing_version": reports.VERSION,
                "report_source_ids": [VEO], "report_source_chat_id": 0}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(caps, rosters, rosters)):
            self.assertTrue(reports.prepare_delivery(item, NOW))
            self.assertFalse(reports.prepare_delivery(item, NOW))
            survey[VEO]["access"][1] = {"b"}
            self.assertTrue(reports.prepare_delivery(item, NOW))
        self.assertEqual(item["payload"]["edit_message_id"], 252)
        self.assertIn("A: 0 active", item["payload"]["text"])
        self.assertIn("B: 1 active · Avg 3.00", item["payload"]["text"])
        self.assertNotIn("BY TEAM", item["payload"]["text"])

    def test_financial_summary_keeps_sales_and_appends_page_averages(self):
        caps = {"members": {str(VEO): [member(1, 3)]}, "page_inputs": fixture_inputs()}
        rosters = {VEO: [{"user_id": 1}]}
        item = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "telegram_message_id": 308, "payload": {
            "organized_summary_date": "2026-10-21", "organized_report_version": reports.VERSION,
            "financial_summary_text": "Existing sales and salary"}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={VEO, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(caps, rosters, rosters)):
            self.assertTrue(reports.prepare_delivery(item, NOW))
        self.assertTrue(item["payload"]["text"].startswith("Existing sales and salary"))
        self.assertIn("Sama Ka Na Media: 1 active · Avg 3.00", item["payload"]["text"])
        self.assertEqual(item["payload"]["edit_message_id"], 308)


if __name__ == "__main__":
    unittest.main()
