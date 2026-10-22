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
    def test_empty_reference_has_no_numeric_fallback_or_buffer(self):
        result = defaults.baseline("suno", [])
        self.assertIsNone(result["capacity_per_day"])
        self.assertIsNone(result["peer_reference_per_day"])
        self.assertEqual(result["peer_members"], 0)
        self.assertTrue(result["reference_only"])
        self.assertNotIn("suno", defaults.SEEDS)

    def test_reference_is_exact_median_of_recorded_active_day_rates(self):
        people = [person(1, [1, 0]), person(2, [3, 0, 0]), person(3, [0, 0]),
                  person(4, [100, 100, 100], "Unknown"), person(5, [100], "Inactive")]
        people += [copy.deepcopy(people[1]), person(None, [100])]
        result = defaults.baseline("suno", people)
        self.assertEqual(result["peer_members"], 3)
        self.assertEqual(result["peer_reference_per_day"], 0.5)
        self.assertIsNone(result["capacity_per_day"])
        self.assertEqual(defaults.peer_label(result), "3 current peers")

    def test_zero_receipts_with_declared_active_date_is_zero_reference_not_capacity(self):
        result = defaults.baseline("suno", [person(1, [0])])
        self.assertEqual(result["peer_reference_per_day"], 0)
        self.assertIsNone(result["capacity_per_day"])

    def test_zero_one_and_two_output_dates_never_inherit_peers_capacity(self):
        reference = defaults.baseline("suno", [person(7, [10, 10, 10])])
        for counts in ([], [1], [10, 20]):
            member = person(1, counts)
            before = copy.deepcopy(member)
            plan = defaults.apply(member, reference)["planning"]
            for key in ("planning_average_per_day", "safe_plan_per_day", "default_capacity_per_day", "buffer_fraction"):
                self.assertIsNone(plan[key])
            self.assertEqual(plan["estimate_origin"], "insufficient_personal_history")
            self.assertFalse(plan["provisional"])
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

    def test_active_members_without_history_are_unknown_not_zero(self):
        reference = defaults.baseline("suno", [person(7, [10, 10, 10])])
        result = reports.forecast([{"user_id": 1}, {"user_id": 2}], [], reference)
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(result["rated"], 0)
        for key in ("average", "total", "conservative"):
            self.assertIsNone(result[key])

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
        self.assertEqual(result["defaults"]["suno"]["peer_members"], 1)

    def test_team_and_general_reports_label_unknown_members_and_comparison(self):
        reference = defaults.baseline("suno", [person(7, [1, 0])])
        roster = [{"user_id": 1}, {"user_id": 2}]
        for chat in (SUNO, MAKATA):
            with self.subTest(chat=chat), patch.dict(reports.AVAILABILITY_GROUPS, {MAKATA: {"name": "Suno Makata"}}):
                text = reports.staffing_text(chat, TODAY, [], roster, [person(1, [1, 2, 3])], reference)
            self.assertIn("History: 1/2 · Unknown: 1", text)
            self.assertIn("Known estimate subtotal: 2.00", text)
            self.assertIn("Missing-history default: Unknown", text)
            self.assertIn("Peer receipt reference: 0.50", text)
            self.assertNotIn("Starting default:", text)
        caps = {"date": str(DAY), "members": {str(SUNO): [person(1, [1, 2, 3])]}, "defaults": {"suno": reference}}
        text = reports.tomorrow_summary(TODAY, {SUNO}, {SUNO: roster}, caps)
        self.assertIn("SUNO: 2 Active · Personal history 1 · Unknown 1", text)
        self.assertIn("Average 2.00/member with history", text)

    def test_detail_report_does_not_repeat_assumed_default_formula(self):
        member = defaults.apply(person(1, [1]), defaults.baseline("suno", []))
        text = reports.capacity_text(SUNO, DAY, [member])
        self.assertIn("Capacity / safe target: Unknown", text)
        self.assertNotIn("Provisional planning:", text)
        self.assertNotIn("Provisional safe = 80%", text)

    def test_makata_staffing_delivery_uses_own_team_reference(self):
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
                patch.object(reports, "staffing_context", return_value=(capacities, {MAKATA: []}, {MAKATA: []})):
            self.assertTrue(reports.prepare_delivery(item, NOW))
        self.assertIn("Peer receipt reference: Unknown", item["payload"]["text"])
        self.assertNotIn("10.00", item["payload"]["text"])

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
                patch.object(makata, "build_report", return_value="Capacity Unknown; peer reference only"), \
                patch.object(makata, "record_automation_marker", side_effect=mark), \
                patch.object(makata, "previous_report_parts", return_value={1: 70, 2: 71}), \
                patch.object(makata, "enqueue_scheduled_action", return_value=True) as outbox:
            self.assertEqual(makata.queue_reports(NOW, {MAKATA, daily.DAILY_REPORTS_CHAT_ID}), 2)
            self.assertEqual(makata.queue_reports(NOW, {MAKATA, daily.DAILY_REPORTS_CHAT_ID}), 0)
        self.assertEqual({c.kwargs["chat_id"] for c in outbox.call_args_list}, {99})
        self.assertEqual(outbox.call_args_list[0].kwargs["payload"]["edit_message_id"], 70)
        self.assertEqual(outbox.call_args_list[1].kwargs["payload"]["edit_message_id"], 71)
        self.assertIn("updated above", outbox.call_args_list[1].kwargs["payload"]["text"])

    def test_makata_private_report_uses_unknown_and_own_peer_reference(self):
        reference = defaults.baseline("suno", [])
        member = defaults.apply(person(1, []), reference)
        capacity = {"members": {str(MAKATA): [member]}, "team_references": {str(MAKATA): reference}}
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
        self.assertIn("Member 1: capacity Unknown · safe target Unknown", text)
        self.assertIn("Peer receipt reference: Unknown", text)
        self.assertNotIn("provisional 0.60", text)


if __name__ == "__main__":
    unittest.main()
