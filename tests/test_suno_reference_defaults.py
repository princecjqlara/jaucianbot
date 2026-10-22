import copy
import datetime as dt
import os
import unittest
from unittest.mock import patch

import capacity_defaults as defaults
import daily_automation as daily
import organized_reports as reports
import recent_capacity
import makata_automation as makata
from test_page_capacity_reports import fixture_inputs

DAY = dt.date(2026, 10, 21)
TODAY = DAY + dt.timedelta(days=1)
NOW = dt.datetime(2026, 10, 22, 2, tzinfo=dt.timezone.utc)
SUNO = daily.TRABAWHO_CHAT_ID
MAKATA = daily.MAKATA_CHAT_ID
VEO = -1003647732254


def person(uid, counts, attendance="Active"):
    start = DAY - dt.timedelta(days=len(counts) - 1)
    member = {"user_id": uid, "name": f"Member {uid}", "daily": [
        {"date": str(start + dt.timedelta(days=i)), "done_deals": n, "attendance": attendance}
        for i, n in enumerate(counts)]}
    member["planning"] = recent_capacity.planning(member, DAY)
    return member


class SunoReferenceDefaultsTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch.object(reports.page_capacity, "load", return_value=fixture_inputs())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.groups = patch.dict(reports.SUNO_GROUPS, {MAKATA: daily.MAKATA})
        self.groups.start()
        self.addCleanup(self.groups.stop)

    def test_empty_reference_has_no_numeric_fallback_or_buffer(self):
        result = defaults.baseline("suno", [])
        self.assertIsNone(result["capacity_per_day"])
        self.assertIsNone(result["peer_reference_per_day"])
        self.assertEqual(result["peer_members"], 0)
        self.assertNotIn("suno", defaults.SEEDS)

    def test_reference_is_exact_median_of_recorded_active_day_rates(self):
        people = [person(1, [1, 0]), person(2, [3, 0, 0]), person(3, [0, 0]),
                  person(4, [100, 100, 100], "Unknown"), person(5, [100], "Inactive"),
                  person(7, [1, 0, 1, 0, 1, 0])]
        people += [copy.deepcopy(people[1]), person(None, [100])]
        result = defaults.baseline("suno", people)
        self.assertEqual(result["peer_members"], 1)
        self.assertEqual(result["reference_peer_members"], 4)
        self.assertEqual(result["peer_reference_per_day"], 0.5)
        self.assertEqual(result["capacity_per_day"], 0.5)
        self.assertEqual(defaults.peer_label(result), "1 current peers")

    def test_zero_receipts_are_preserved_as_observation_without_inventing_a_repeated_output_default(self):
        result = defaults.baseline("suno", [person(1, [0])])
        self.assertEqual(result["peer_reference_per_day"], 0)
        self.assertIsNone(result["capacity_per_day"])
        plan = defaults.apply(person(2, []), result)["planning"]
        self.assertIsNone(plan["planning_average_per_day"])
        self.assertFalse(plan["provisional"])

    def test_zero_one_and_two_output_dates_use_peer_plan_without_inventing_personal_capacity(self):
        reference = defaults.baseline("suno", [person(7, [10, 10, 10])])
        for counts in ([], [1], [10, 20]):
            member = person(1, counts)
            before = copy.deepcopy(member)
            plan = defaults.apply(member, reference)["planning"]
            for key in ("estimated_capacity_per_day", "safe_plan_per_day", "buffer_fraction"):
                self.assertIsNone(plan[key])
            self.assertEqual(plan["planning_average_per_day"], 10)
            self.assertEqual(plan["default_capacity_per_day"], 10)
            self.assertEqual(plan["estimate_origin"], "historical_peer_default")
            self.assertTrue(plan["provisional"])
            self.assertEqual(member, before)

    def test_old_cached_provisional_seed_and_buffer_are_removed(self):
        member = person(1, [])
        member["planning"].update(planning_average_per_day=0.6, safe_plan_per_day=0.48,
            provisional=True, default_capacity_per_day=0.6)
        plan = defaults.apply(member, defaults.baseline("suno", []))["planning"]
        self.assertIsNone(plan["planning_average_per_day"])
        self.assertIsNone(plan["safe_plan_per_day"])
        self.assertIsNone(plan["default_capacity_per_day"])

    def test_personal_estimate_remains_personal_when_history_is_sufficient(self):
        member = person(1, [1, 2, 3])
        plan = defaults.apply(member, defaults.baseline("suno", []))["planning"]
        self.assertEqual(plan["planning_average_per_day"], 2)
        self.assertEqual(plan["estimate_origin"], "personal_history")
        self.assertFalse(plan["provisional"])

    def test_active_members_without_history_use_peer_plan_in_total_and_average(self):
        reference = defaults.baseline("suno", [person(7, [10, 10, 10])])
        result = reports.forecast([{"user_id": 1}, {"user_id": 2}], [], reference)
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["unknown"], 0)
        self.assertEqual(result["rated"], 0)
        self.assertEqual(result["provisional"], 2)
        self.assertEqual(result["average"], 10)
        self.assertEqual(result["total"], 20)
        self.assertIsNone(result["conservative"])

    def test_partial_subtotal_and_average_only_cover_personal_history(self):
        reference = defaults.baseline("suno", [])
        result = reports.forecast([{"user_id": 1}, {"user_id": 2}], [person(1, [1, 2, 3])], reference)
        self.assertEqual(result["rated"], 1)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["average"], 2)
        self.assertEqual(result["conservative"], 1)
        self.assertEqual(reports.forecast([], [], reference)["total"], 0)
        self.assertIsNone(reports.forecast(None, [], reference)["total"])

    def test_makata_reference_uses_own_attendance_not_another_teams_votes(self):
        raw = {"date": str(DAY), "members": {str(SUNO): [person(1, [1, 1, 1])],
            str(MAKATA): [person(1, [0, 0, 1], "Unknown")]}}
        with patch.dict(daily.SUNO_GROUPS, {MAKATA: {"name": "Suno Makata"}}):
            result = defaults.enrich(raw, {SUNO, MAKATA})
        self.assertIsNone(result["team_references"][str(MAKATA)]["peer_reference_per_day"])
        self.assertEqual(result["team_references"][str(SUNO)]["peer_reference_per_day"], 1)
        # Same person's real cross-team receipt history is retained separately.
        self.assertEqual(result["members"][str(MAKATA)][0]["planning"]["estimated_capacity_per_day"], 4 / 3)

    def test_current_membership_filter_applies_before_peer_reference(self):
        raw = {"date": str(DAY), "members": {str(SUNO): [person(1, [1, 0]), person(2, [100, 100])]}}
        with patch.object(reports.member_presence, "filter_current", return_value=(raw["members"][str(SUNO)][:1], {})):
            result = reports.current_capacities(raw, {SUNO}, NOW)
        self.assertEqual(result["defaults"]["suno"]["peer_reference_per_day"], 0.5)
        self.assertEqual(result["defaults"]["suno"]["peer_members"], 0)
        self.assertEqual(result["defaults"]["suno"]["reference_peer_members"], 1)

    def test_team_and_general_reports_include_provisional_members_without_assumed_safe_target(self):
        reference = defaults.baseline("suno", [person(7, [1, 1, 1])])
        roster = [{"user_id": 1}, {"user_id": 2}]
        for chat in (SUNO, MAKATA):
            with self.subTest(chat=chat), patch.dict(reports.AVAILABILITY_GROUPS, {MAKATA: {"name": "Suno Makata"}}):
                text = reports.staffing_text(chat, TODAY, [], roster, [person(1, [1, 2, 3])], reference)
            self.assertIn("Personal history: 1 · Provisional: 1 · Unknown: 0", text)
            self.assertIn("Estimated total: 3.00 · Safe target: Unknown", text)
            self.assertIn("Suno new-member default: 1.00", text)
            self.assertIn("2 active · Avg 1.50", text)
            self.assertNotIn("Starting default:", text)
        caps = {"date": str(DAY), "members": {str(SUNO): [person(1, [1, 2, 3])]}, "defaults": {"suno": reference}}
        text = reports.tomorrow_summary(TODAY, {SUNO}, {SUNO: roster}, caps)
        self.assertIn("Hiraya Studio: 2 active · Avg 1.50 · 1 provisional", text)
        self.assertIn("SUNO · receipts/day", text)

    def test_detail_report_does_not_repeat_assumed_default_formula(self):
        member = defaults.apply(person(1, [1]), defaults.baseline("suno", []))
        text = reports.capacity_text(SUNO, DAY, [member])
        self.assertIn("Capacity / safe target: Unknown", text)
        self.assertNotIn("Provisional planning:", text)
        self.assertNotIn("Provisional safe = 80%", text)

    def test_makata_staffing_uses_shared_suno_default_when_own_history_is_missing(self):
        business = defaults.baseline("suno", [person(7, [10, 10, 10])])
        own = defaults.baseline("suno", [])
        capacities = {"members": {str(MAKATA): []}, "businesses": {"suno": []},
            "defaults": {"suno": business}, "team_references": {str(MAKATA): own}}
        item = {"chat_id": daily.DAILY_REPORTS_CHAT_ID, "payload": {
            "organized_staffing_date": str(TODAY), "organized_staffing_version": reports.VERSION,
            "report_source_ids": [MAKATA], "report_source_chat_id": MAKATA}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.dict(reports.AVAILABILITY_GROUPS, {MAKATA: {"name": "Suno Makata"}}), \
                patch.object(reports, "allowed_chat_ids", return_value={MAKATA, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "staffing_context", return_value=(capacities, {MAKATA: []}, {MAKATA: [{"user_id": 1}]})):
            self.assertTrue(reports.prepare_delivery(item, NOW))
        self.assertIn("Suno new-member default: 10.00", item["payload"]["text"])
        self.assertIn("Estimated total: 10.00 · Safe target: Unknown", item["payload"]["text"])
        self.assertIn("Makata Studios: 1 active · Avg 10.00", item["payload"]["text"])

    def test_makata_private_daily_correction_edits_existing_report_and_removes_old_parts(self):
        saved = {}
        def save(key, value, now):
            saved[key] = value
            return value
        def mark(chat, key, now):
            saved[key] = {}
            return True
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true", "SUNO_MAKATA_ENABLED": "true"}), \
                patch.dict(daily.SUNO_GROUPS, {MAKATA: daily.MAKATA}), \
                patch.object(makata, "recipient", return_value={"chat_id": 99}), \
                patch.object(makata, "_read", side_effect=saved.get), patch.object(makata, "_save", side_effect=save), \
                patch.object(makata, "build_report", return_value="Provisional historical peer default"), \
                patch.object(makata, "record_automation_marker", side_effect=mark), \
                patch.object(makata, "previous_report_parts", return_value={1: 70, 2: 71}), \
                patch.object(makata, "enqueue_scheduled_action", return_value=True) as outbox:
            self.assertEqual(makata.queue_reports(NOW.replace(hour=15, minute=59), {MAKATA, daily.DAILY_REPORTS_CHAT_ID}), 2)
            self.assertEqual(makata.queue_reports(NOW.replace(hour=15, minute=59), {MAKATA, daily.DAILY_REPORTS_CHAT_ID}), 0)
        self.assertEqual({c.kwargs["chat_id"] for c in outbox.call_args_list}, {99})
        self.assertEqual(outbox.call_args_list[0].kwargs["payload"]["edit_message_id"], 70)
        self.assertEqual(outbox.call_args_list[1].kwargs["payload"]["edit_message_id"], 71)
        self.assertIn("updated above", outbox.call_args_list[1].kwargs["payload"]["text"])

    def test_makata_private_report_uses_provisional_shared_suno_default(self):
        reference = defaults.baseline("suno", [person(7, [1, 1, 1])])
        member = defaults.apply(person(1, []), reference)
        capacity = {"members": {str(MAKATA): [member]}, "defaults": {"suno": reference},
            "team_references": {str(MAKATA): defaults.baseline("suno", [])}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.dict(daily.SUNO_GROUPS, {MAKATA: daily.MAKATA}), \
                patch.object(makata, "receipt_messages", return_value=[]), \
                patch.object(makata, "daily_poll_counts", return_value={}), \
                patch("trabawho_songs.song_report_lines", return_value=[]), \
                patch("suno_capacity.report", return_value={"members": []}), \
                patch("cloud_store.allowed_chat_ids", return_value={MAKATA, daily.DAILY_REPORTS_CHAT_ID}), \
                patch.object(reports, "load_capacity", return_value={}), \
                patch.object(reports, "current_capacities", return_value=capacity):
            text = makata.build_report(DAY, NOW)
        self.assertIn("Member 1: provisional 1.00/day · safe pending", text)
        self.assertIn("Suno new-member default: 1.00", text)
        self.assertNotIn("safe 0.40", text)

    def test_default_refreshes_from_actual_receipts_without_a_fixed_seed_or_provisional_feedback(self):
        people = [person(1, [3, 2, 1]), person(2, [8, 4, 2])]
        first = defaults.baseline("suno", people)
        self.assertAlmostEqual(first["capacity_per_day"], 10 / 3)
        new = defaults.apply(person(3, []), first)
        self.assertAlmostEqual(defaults.baseline("suno", people + [new])["capacity_per_day"], 10 / 3)
        refreshed = defaults.baseline("suno", [person(1, [2, 1, 1]), person(2, [1, 1, 1])])
        self.assertAlmostEqual(refreshed["capacity_per_day"], 7 / 6)

    def test_third_personal_output_date_replaces_suno_default(self):
        reference = defaults.baseline("suno", [person(7, [1, 1, 1])])
        self.assertEqual(defaults.apply(person(1, [4, 3]), reference)["planning"]["planning_average_per_day"], 1)
        plan = defaults.apply(person(1, [4, 3, 2]), reference)["planning"]
        self.assertEqual(plan["planning_average_per_day"], 3)
        self.assertEqual(plan["safe_plan_per_day"], 2)
        self.assertFalse(plan["provisional"])

    def test_established_active_day_default_uses_recorded_zeros_and_resists_peaks(self):
        people = [person(i, list(counts) + [0] * 4) for i, counts in enumerate(
            ([2, 2, 1], [4, 1, 1], [1, 1, 1], [2, 1, 1], [2, 2, 2], [6, 3, 3], [3, 2, 2]))]
        people += [person(20, [1000, 1000]), person(21, [1000, 1000, 1000], "Inactive")]
        result = defaults.baseline("suno", people)
        self.assertEqual(result["peer_members"], 7)
        self.assertAlmostEqual(result["capacity_per_day"], 6 / 7)
        self.assertEqual(result["source"], "established_peer_active_day_median")

    def test_reviewed_suno_peer_active_day_rates_produce_point_six_without_a_fixed_seed(self):
        counts = ([2, 2, 1, 0, 0, 0, 0], [4, 1, 1, 1] + [0] * 9,
            [1, 1, 1, 0, 0, 0, 0], [1, 1, 0, 0], [2, 2, 2] + [0] * 6,
            [2, 1, 1, 0], [1, 1, 1, 0, 0])
        people = [person(i, values) for i, values in enumerate(counts)]
        # A receipt on an Unknown date can supply output-history evidence but
        # must not enter the declared Active-day numerator or denominator.
        people[3]["daily"].insert(0, {"date": str(DAY - dt.timedelta(days=4)),
            "done_deals": 2, "attendance": "Unknown"})
        people[3]["planning"] = recent_capacity.planning(people[3], DAY)
        result = defaults.baseline("suno", people)
        self.assertEqual(result["peer_members"], 7)
        self.assertEqual(result["capacity_per_day"], 0.6)
        plan = defaults.apply(person(90, []), result)["planning"]
        self.assertEqual(plan["planning_average_per_day"], 0.6)
        self.assertIsNone(plan["safe_plan_per_day"])

    def test_no_established_peers_means_unknown_instead_of_a_guessed_lower_number(self):
        result = defaults.baseline("suno", [person(1, [1, 0]), person(2, [20, 20, 20], "Unknown")])
        self.assertIsNone(result["capacity_per_day"])


if __name__ == "__main__":
    unittest.main()
