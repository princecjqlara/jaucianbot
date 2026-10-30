import copy
import datetime as dt
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import app
import capacity_defaults
import daily_automation as daily
import makata_automation as makata
import nightly_reports as nightly
import organized_reports as reports
import recent_capacity
import veo_forecast
from test_page_capacity_reports import fixture_inputs

VEO = -1003647732254
TARGET = daily.DAILY_REPORTS_CHAT_ID
DAY = dt.date(2026, 10, 22)
NIGHT = dt.datetime(2026, 10, 22, 15, 59, tzinfo=dt.timezone.utc)


def person(uid, rate):
    return {"user_id": uid, "name": f"Member {uid}", "planning": {
        "policy": veo_forecast.POLICY, "known_active_days": 3 if rate is not None else 0,
        "uncalibrated_forecast_per_day": rate,
        "estimated_capacity_per_day": rate, "planning_average_per_day": rate,
        "safe_plan_per_day": 1 if rate is not None else None,
        "top_three_daily_outputs": [2, 2, 2] if rate is not None else [],
        "eligible_output_days": 3 if rate is not None else 0,
        "window_end": "2026-10-21"}}


class NightlyTimingTests(unittest.TestCase):
    def test_only_2359_pht_starts_the_daily_batch(self):
        for hours, minutes in ((0, 0), (3, 59), (12, 0), (15, 58)):
            with self.subTest(utc_hour=hours, utc_minute=minutes):
                self.assertIsNone(nightly.report_date(NIGHT.replace(hour=hours, minute=minutes)))
        self.assertEqual(nightly.report_date(NIGHT), DAY)
        self.assertEqual(nightly.report_date(NIGHT + dt.timedelta(seconds=59)), DAY)

    def test_midnight_recovery_keeps_the_original_report_date(self):
        self.assertEqual(nightly.report_date(NIGHT + dt.timedelta(minutes=1)), DAY)
        self.assertEqual(nightly.report_date(NIGHT + dt.timedelta(minutes=59)), DAY)
        self.assertIsNone(nightly.report_date(NIGHT + dt.timedelta(minutes=60)))

    def test_old_five_minute_public_checks_are_retired_without_delivery(self):
        for payload in ({"organized_staffing_date": str(DAY)}, {"organized_summary_date": str(DAY)}):
            action = {"chat_id": TARGET, "repeat_interval_minutes": 5, "payload": payload}
            self.assertFalse(nightly.prepare_delivery(action, NIGHT.replace(hour=8)))
            self.assertIsNone(action["repeat_interval_minutes"])

    def test_automatic_private_reports_follow_the_same_nightly_window(self):
        action = {"chat_id": 99, "payload": {"makata_private_report": True, **nightly.payload(DAY)}}
        self.assertFalse(nightly.prepare_delivery(copy.deepcopy(action), NIGHT - dt.timedelta(minutes=1)))
        self.assertTrue(nightly.prepare_delivery(copy.deepcopy(action), NIGHT))
        self.assertTrue(nightly.prepare_delivery(copy.deepcopy(action), NIGHT + dt.timedelta(minutes=4)))

    def test_stale_batch_cannot_be_sent_the_following_afternoon(self):
        action = {"chat_id": TARGET, "payload": {"organized_report_date": str(DAY), **nightly.payload(DAY)}}
        self.assertFalse(nightly.prepare_delivery(action, NIGHT + dt.timedelta(days=1)))

    def test_explicit_requested_reports_and_assignment_messages_are_unaffected(self):
        for action in ({"chat_id": VEO, "payload": {"new_client_token": "abc"}},
                {"chat_id": 99, "payload": {"makata_private_report": True, "makata_report_requested": True}}):
            self.assertTrue(nightly.prepare_delivery(action, NIGHT.replace(hour=8)))


class NightlyQueueTests(unittest.TestCase):
    def fixture(self, stack):
        saved, actions = {}, {}
        raw = {"date": "2026-10-21", "members": {str(VEO): [person(1, 2)]}}
        caps = capacity_defaults.enrich(raw, {VEO})
        stack.enter_context(patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}))
        stack.enter_context(patch.object(reports, "_read", side_effect=lambda key: saved.get(key)))
        stack.enter_context(patch.object(reports, "_save", side_effect=lambda key, value, now: saved.setdefault(key, value)))
        load = stack.enter_context(patch.object(reports, "load_capacity", return_value=raw))
        stack.enter_context(patch.object(reports, "current_capacities", return_value=caps))
        stack.enter_context(patch.object(reports, "previous_messages", return_value={}))
        stack.enter_context(patch.object(reports, "previous_texts", return_value={}))
        stack.enter_context(patch.object(reports, "previous_staffing_id", return_value=None))
        stack.enter_context(patch.object(reports.concern_reports, "snapshot", return_value={"groups": []}))
        stack.enter_context(patch.object(reports.page_capacity, "load", return_value=fixture_inputs()))
        stack.enter_context(patch.object(reports, "_rosters", return_value={VEO: [{"user_id": 1}]}))
        finance = {"gross": "0", "salary": "0", "net": "0", "count": 0, "workers": [],
            "partial": False, "review": [], "salary_basis": "Recorded earnings."}
        finances = stack.enter_context(patch.object(reports, "finances", return_value=finance))
        def enqueue(**action):
            if action["dedupe_key"] in actions:
                return False
            actions[action["dedupe_key"]] = action
            return True
        stack.enter_context(patch.object(reports, "enqueue_scheduled_action", side_effect=enqueue))
        return saved, actions, load, finances

    def test_daytime_cannot_queue_or_edit_daily_reports(self):
        with ExitStack() as stack:
            _, actions, load, finances = self.fixture(stack)
            self.assertEqual(reports.queue_automation(NIGHT.replace(hour=8), {VEO, TARGET}), 0)
            self.assertEqual(actions, {})
            load.assert_not_called()
            finances.assert_not_called()

    def test_history_prewarms_before_2359_without_outgoing_reports(self):
        with ExitStack() as stack:
            _, actions, load, finances = self.fixture(stack)
            self.assertEqual(reports.queue_automation(NIGHT.replace(minute=50), {VEO, TARGET}), 0)
            self.assertEqual(actions, {})
            load.assert_called_once_with(DAY - dt.timedelta(days=1), NIGHT.replace(minute=50), {VEO, TARGET})
            finances.assert_not_called()

    def test_nightly_reports_cover_today_and_tomorrow_with_completed_capacity_dates(self):
        with ExitStack() as stack:
            saved, actions, load, finances = self.fixture(stack)
            self.assertGreater(reports.queue_automation(NIGHT, {VEO, TARGET}), 0)
            self.assertEqual(reports.queue_automation(NIGHT + dt.timedelta(seconds=20), {VEO, TARGET}), 0)
            self.assertEqual(reports.queue_automation(NIGHT + dt.timedelta(minutes=5), {VEO, TARGET}), 0)
        finances.assert_called_once_with(VEO, DAY)
        load.assert_called_once_with(DAY - dt.timedelta(days=1), NIGHT, {VEO, TARGET})
        for action in actions.values():
            self.assertIsNone(action["repeat_interval_minutes"])
            self.assertEqual(action["payload"]["nightly_publication_date"], str(DAY))
            self.assertNotIn("organized_summary_date", action["payload"])
            self.assertNotIn("organized_staffing_date", action["payload"])
            self.assertGreaterEqual(action["scheduled_for"], NIGHT)
            self.assertTrue(nightly.prepare_delivery(action, NIGHT + dt.timedelta(minutes=2)))
        summary = next(a["payload"]["text"] for a in actions.values() if a["payload"].get("report_category") == "summary")
        self.assertIn("2026-10-22 (PHT)", summary)
        self.assertIn("2026-10-23 (PHT)", summary)
        self.assertIn("through 2026-10-21", summary)
        capacity = next(a["payload"]["text"] for a in actions.values() if a["payload"].get("report_category") == "capacity")
        self.assertIn("Nightly baseline: 2.00/day", capacity)
        self.assertIn("2026-10-08 to 2026-10-21", capacity)

    def test_long_nightly_summary_is_split_without_dropping_content_or_duplicate_retries(self):
        financial = "DAILY SALES & SALARY SUMMARY\n" + "Summary row with emoji 🚀\n" * 500
        with ExitStack() as stack:
            _, actions, _, _ = self.fixture(stack)
            stack.enter_context(patch.object(reports, "summary_text", return_value=financial))
            self.assertGreater(reports.queue_automation(NIGHT, {VEO, TARGET}), 0)
            self.assertEqual(reports.queue_automation(NIGHT + dt.timedelta(seconds=20), {VEO, TARGET}), 0)
        summaries = [a for a in actions.values() if a["payload"].get("report_category") == "summary"]
        self.assertGreater(len(summaries), 1)
        self.assertEqual(len({a["dedupe_key"] for a in summaries}), len(summaries))
        text = "\n".join(a["payload"]["text"] for a in summaries)
        self.assertEqual(text.count("Summary row with emoji 🚀"), 500)
        self.assertIn("TOMORROW CAPACITY BY PAGE", text)
        for action in summaries:
            self.assertLessEqual(len(action["payload"]["text"].encode("utf-16-le")) // 2, 4096)
            self.assertEqual(action["payload"]["nightly_publication_date"], str(DAY))

    def test_first_attempt_after_midnight_reports_yesterday_and_its_next_day(self):
        with ExitStack() as stack:
            _, actions, _, finances = self.fixture(stack)
            reports.queue_automation(NIGHT + dt.timedelta(minutes=3), {VEO, TARGET})
        finances.assert_called_once_with(VEO, DAY)
        self.assertTrue(all(a["payload"].get("nightly_publication_date") == str(DAY) for a in actions.values()))
        summary = next(a["payload"]["text"] for a in actions.values() if a["payload"].get("report_category") == "summary")
        self.assertIn("2026-10-23 (PHT)", summary)
        self.assertNotIn("2026-10-24", summary)

    def test_makata_waits_until_2359_and_does_not_build_a_daytime_report(self):
        with patch.object(makata, "enabled", return_value=True), patch.object(makata, "recipient") as recipient, \
                patch.object(makata, "build_report") as build, patch("automation_controls.chat_enabled", return_value=True):
            self.assertEqual(makata.queue_reports(NIGHT.replace(hour=8), {daily.MAKATA_CHAT_ID, TARGET}), 0)
        recipient.assert_not_called()
        build.assert_not_called()

    def test_makata_nightly_batch_uses_the_current_date_and_stops_after_one_queue(self):
        saved, actions = {}, {}
        def save(key, value, now):
            return saved.setdefault(key, value)
        def marker(chat, key, now):
            saved[key] = {}
            return True
        def enqueue(**action):
            actions[action["dedupe_key"]] = action
            return True
        with patch.object(makata, "enabled", return_value=True), \
                patch("automation_controls.chat_enabled", return_value=True), \
                patch.object(makata, "recipient", return_value={"chat_id": 99}), \
                patch.object(makata, "_read", side_effect=saved.get), patch.object(makata, "_save", side_effect=save), \
                patch.object(makata, "build_report", return_value="Makata nightly report") as build, \
                patch.object(makata, "previous_report_parts", return_value={}), \
                patch.object(makata, "record_automation_marker", side_effect=marker), \
                patch.object(makata, "enqueue_scheduled_action", side_effect=enqueue):
            self.assertGreater(makata.queue_reports(NIGHT, {daily.MAKATA_CHAT_ID, TARGET}), 0)
            count = len(actions)
            self.assertEqual(makata.queue_reports(NIGHT + dt.timedelta(minutes=5), {daily.MAKATA_CHAT_ID, TARGET}), 0)
        build.assert_called_once_with(DAY, NIGHT)
        self.assertEqual(len(actions), count)
        for action in actions.values():
            self.assertEqual(action["payload"]["nightly_publication_date"], str(DAY))
            self.assertEqual(action["payload"]["makata_report_date"], str(DAY))
            self.assertNotIn("repeat_interval_minutes", action)

    def test_personal_capacity_changes_are_written_in_each_member_report(self):
        with ExitStack() as stack:
            saved, actions, _, _ = self.fixture(stack)
            previous = {"businesses": {"veo": [person(1, 1)]}}
            saved[f"nightly-personal-capacity:{DAY - dt.timedelta(days=1)}:{reports._scope({VEO})}"] = previous
            reports.queue_automation(NIGHT, {VEO, TARGET})
        detail = next(a["payload"]["text"] for a in actions.values() if a["payload"].get("report_category") == "capacity")
        self.assertIn("Forecast change: Increased: 1.00 → 2.00/day", detail)
        self.assertIn("nightly-personal-capacity", str(saved))


class CapacityChangeTests(unittest.TestCase):
    def test_increase_decrease_unchanged_and_new_personal_estimates(self):
        before = {"businesses": {"veo": [person(1, 1), person(2, 3), person(3, 2), person(4, None)]}}
        now = {"businesses": {"veo": [person(1, 2), person(2, 2), person(3, 2), person(4, 1), person(5, None)]}}
        result = nightly.changes(now, before)
        self.assertIn("Increased: 1.00 → 2.00", result["veo", 1]["text"])
        self.assertIn("Decreased: 3.00 → 2.00", result["veo", 2]["text"])
        self.assertIn("Unchanged: 2.00", result["veo", 3]["text"])
        self.assertIn("Personal estimate now available", result["veo", 4]["text"])
        self.assertIn("Provisional", result["veo", 5]["text"])

    def test_same_person_in_veo_and_suno_has_separate_comparisons(self):
        old = {"businesses": {"veo": [person(1, 3)], "suno": [person(1, 1)]}}
        now = {"businesses": {"veo": [person(1, 3)], "suno": [person(1, 2)]}}
        result = nightly.changes(now, old)
        self.assertIn("Unchanged", result["veo", 1]["text"])
        self.assertIn("Increased", result["suno", 1]["text"])

    def test_loss_of_recent_history_stays_provisional_instead_of_claiming_decreased_ability(self):
        result = nightly.changes({"businesses": {"veo": [person(1, None)]}},
            {"businesses": {"veo": [person(1, 3)]}})
        self.assertIn("Provisional", result["veo", 1]["text"])
        self.assertNotIn("Decreased", result["veo", 1]["text"])

    def test_rolling_history_can_increase_or_decrease_without_changing_the_formula(self):
        end = dt.date(2026, 10, 21)
        daily_records = [{"date": str(end - dt.timedelta(days=13) + dt.timedelta(days=i)),
            "done_deals": n, "attendance": "Active"} for i, n in enumerate([5, 4, 3] + [1] * 11)]
        before = recent_capacity.planning({"daily": daily_records}, end)["estimated_capacity_per_day"]
        after = recent_capacity.planning({"daily": daily_records}, end + dt.timedelta(days=1))["estimated_capacity_per_day"]
        self.assertEqual(before, 4)
        self.assertLess(after, before)


if __name__ == "__main__":
    unittest.main()
