import copy
import datetime as dt
import os
import unittest
from unittest.mock import patch

import app
import organized_reports as reports
import report_topics
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET, TRABAWHO_CHAT_ID as SUNO, GROUPS
from test_wsgi import call_app

VEO = -1003647732254
DAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 17, tzinfo=dt.timezone.utc)


class OrganizedReportsTests(unittest.TestCase):
    def test_report_bursts_use_recent_durable_deliveries_and_guarded_deferral(self):
        action = {"id": 99, "chat_id": TARGET, "status": "processing", "updated_at": NOW.isoformat(),
            "payload": {"_delivery_failures": 1}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(report_topics, "request", return_value=[{"id": n} for n in range(12)]) as read, \
                patch("cloud_store._update_claim", return_value=True) as update:
            self.assertTrue(report_topics.defer_burst(action, NOW))
        self.assertIn("sent_at=gte.", read.call_args.args[0])
        from postgres_archive import compile_request
        statement, values, shape = compile_request(read.call_args.args[0])
        self.assertEqual(shape, "rows")
        self.assertIn('"a"."telegram_message_id" > %s', statement.as_string())
        changes = update.call_args.args[1]
        self.assertEqual(changes["status"], "pending")
        self.assertEqual(changes["attempts"], 1)
        self.assertEqual(changes["scheduled_for"], (NOW + dt.timedelta(seconds=90)).isoformat())
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(report_topics, "request", return_value=[]), \
                patch("cloud_store._update_claim") as update:
            self.assertIsNone(report_topics.defer_burst(action, NOW))
        update.assert_not_called()

    def test_topics_endpoint_requires_auth_and_get_is_read_only(self):
        with patch.dict(os.environ, {"INSIGHTS_API_KEY": "key", "ALLOWED_CHAT_IDS": str(TARGET)}), \
                patch.object(report_topics, "topics", return_value=[{"thread_id": 195, "name": "Veo"}]) as read:
            self.assertEqual(call_app("/api/reports/topics")[0], 401)
            status, result = call_app("/api/reports/topics", headers={"Authorization": "Bearer key"})
        self.assertEqual(status, 200)
        self.assertTrue(result["read_only"])
        read.assert_called_once()

    def test_verified_team_topics_and_legacy_retirement_preserve_private_reports(self):
        self.assertEqual(report_topics.TEAM_THREADS[VEO], 195)
        self.assertEqual(report_topics.TEAM_THREADS[SUNO], 205)
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}):
            old = {"chat_id": TARGET, "dedupe_key": "tomorrow-veo-capacity:v1:old", "repeat_interval_minutes": 5}
            self.assertFalse(report_topics.delivery_allowed(old))
            self.assertIsNone(old["repeat_interval_minutes"])
            self.assertTrue(report_topics.delivery_allowed({"chat_id": 123, "dedupe_key": "makata-daily-report:old"}))

    def test_receipt_finance_counts_outside_bot_clients_and_written_salary(self):
        row = {"chat_id": SUNO, "message_id": 42, "author_id": 7, "author_name": "Alex",
               "sent_utc": "2026-10-21T02:00:00Z", "thread_id": 4, "source": "bot", "text": "HIRAYA\n500(100)"}
        with patch.object(reports, "receipt_messages", return_value=[row]):
            result = reports.finances(SUNO, DAY)
        self.assertEqual(result["gross"], "500")
        self.assertEqual(result["salary"], "100")
        self.assertEqual(result["net"], "400")
        text = reports.daily_sections(SUNO, DAY, result, [], [], False)
        self.assertIn("Alex: ₱100", text["salary"])
        self.assertIn("Sales less salary: ₱400", text["salary"])
        self.assertIn("coverage is incomplete", text["problems"])

    def test_veo_missing_approved_salary_stays_unknown(self):
        finance = {"gross": "1000", "salary": None, "net": None, "count": 1, "unit": "Done Deals",
            "workers": [{"name": "A", "count": 1, "gross": "1000", "salary": None}], "partial": False,
            "review": [], "salary_basis": "Pending approved salary records."}
        text = reports.daily_sections(VEO, DAY, finance, [], [], True)
        self.assertIn("Total salary: pending", text["salary"])
        self.assertNotIn("₱0", text["salary"])
        self.assertIn("No archived concern posts", text["problems"])

    def test_forecast_uses_exact_means_unique_ids_and_excludes_unknowns(self):
        members = [{"user_id": 7, "name": "A", "planning": {"planning_average_per_day": 1 / 3,
            "safe_plan_per_day": 0.8 / 3}}, {"user_id": 8, "name": "B", "planning": {}}]
        result = reports.forecast([{"user_id": 7}, {"user_id": 7}, {"user_id": 8}], members)
        self.assertEqual(result["active"], 2)
        self.assertEqual(result["rated"], 1)
        self.assertEqual(result["average"], 1 / 3)
        self.assertEqual(result["conservative"], 0.8 / 3)
        self.assertIsNone(reports.forecast(None, members)["active"])
        self.assertEqual(reports.forecast([], members)["active"], 0)
        self.assertIsNone(reports.forecast([], members)["average"])

    def test_capacity_keeps_zero_active_days_and_receipt_basis(self):
        member = {"name": "A", "planning": {"active_day_average": 0.5, "safe_plan_per_day": None,
            "known_active_days": 4, "eligible_output_days": 2, "output_day_average": 1.0, "observed_daily_peak": 1}}
        text = reports.capacity_text(SUNO, DAY, [member])
        self.assertIn("Active-day average: 0.50", text)
        self.assertIn("including zero output", text)
        self.assertIn("pending (2/3 output dates)", text)
        self.assertIn("non-bot clients", text)
        self.assertNotIn("delivery dates", text)

    def test_staffing_board_edits_when_votes_change_and_is_quiet_when_unchanged(self):
        teams = {VEO}
        action = {"chat_id": TARGET, "payload": {"organized_staffing_date": "2026-10-22",
            "organized_staffing_version": reports.VERSION,
            "report_source_ids": [VEO], "report_source_chat_id": VEO}, "repeat_interval_minutes": 5}
        capacity = {"members": {str(VEO): [{"user_id": 7, "name": "A", "planning": {
            "planning_average_per_day": 1.5, "safe_plan_per_day": 1.2}}]}}
        roster = {VEO: [{"user_id": 7}]}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={TARGET, VEO}), \
                patch.object(reports, "_read", return_value=capacity), \
                patch.object(reports, "current_capacities", return_value=capacity), \
                patch.object(reports, "_rosters", return_value=roster):
            self.assertTrue(reports.prepare_delivery(action, NOW))
            action["telegram_message_id"] = 500
            self.assertFalse(reports.prepare_delivery(action, NOW))
            action["payload"]["_organized_fingerprint"] = "old"
            self.assertTrue(reports.prepare_delivery(action, NOW))
        self.assertEqual(action["payload"]["edit_message_id"], 500)
        self.assertIn("Tomorrow estimated capacity: 1.50", action["payload"]["text"])
        self.assertIn("2026-10-23", action["payload"]["text"])

    def test_expired_staffing_board_stops_recurring(self):
        action = {"chat_id": TARGET, "payload": {"organized_staffing_date": "2026-10-21",
            "report_source_ids": [VEO]}, "repeat_interval_minutes": 5}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(reports, "allowed_chat_ids", return_value={TARGET, VEO}):
            self.assertFalse(reports.prepare_delivery(action, NOW))
        self.assertIsNone(action["repeat_interval_minutes"])

    def test_daily_reports_are_idempotent_and_use_team_topic_without_enabling_team_bot(self):
        saved = {}
        capacity = {"members": {str(VEO): []}}
        finance = {"gross": "1000", "salary": None, "net": None, "count": 1, "unit": "Done Deals",
            "workers": [], "review": [], "partial": False, "salary_basis": "Pending approved salary records."}
        def save(key, value, now):
            return saved.setdefault(key, value)
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true", "AUTOMATION_CHAT_IDS": str(TARGET)}), \
                patch.object(reports, "_read", side_effect=lambda key: saved.get(key)), \
                patch.object(reports, "_save", side_effect=save), \
                patch.object(reports, "load_capacity", return_value=capacity), \
                patch.object(reports, "previous_messages", return_value={}), \
                patch.object(reports, "previous_texts", return_value={}), \
                patch.object(reports, "previous_staffing_id", return_value=None), \
                patch.object(reports.concern_reports, "snapshot", return_value={"groups": []}), \
                patch.object(reports, "finances", return_value=finance), \
                patch.object(reports, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(reports.queue_automation(NOW, {VEO, TARGET}), 8)
            self.assertEqual(reports.queue_automation(NOW, {VEO, TARGET}), 0)
            self.assertFalse(reports.automation_controls.chat_enabled(VEO))
        calls = [call.kwargs for call in queue.call_args_list]
        self.assertTrue(all(c["chat_id"] == TARGET for c in calls))
        team = [c for c in calls if c["payload"].get("report_source_chat_id") == VEO]
        self.assertEqual(len(team), 5)
        self.assertTrue(all(c["payload"]["message_thread_id"] == 195 for c in team))
        self.assertEqual({c["payload"].get("report_category") for c in team if c["payload"].get("report_category")},
                         {"sales", "salary", "capacity", "problems"})


if __name__ == "__main__":
    unittest.main()
