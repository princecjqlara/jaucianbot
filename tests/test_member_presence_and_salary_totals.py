import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch

import member_presence as presence
import organized_reports as reports
import recent_capacity
import report_topics
import telegram_sender
from daily_automation import DAILY_REPORTS_CHAT_ID as TARGET, TRABAWHO_CHAT_ID as SUNO
from test_wsgi import call_app

CHAT = -1003647732254
DAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 20, tzinfo=dt.timezone.utc)


def record(uid, status="member", **extra):
    return {"user_id": uid, "status": status, "current": status == "member",
            "verified_at": NOW.isoformat(), "expires_at": (NOW + dt.timedelta(minutes=5)).isoformat(), **extra}


class CurrentMembershipTests(unittest.TestCase):
    def test_statuses_require_current_membership_including_restricted_flag(self):
        for status in ("creator", "administrator", "member"):
            self.assertTrue(presence.is_current({"status": status}))
        self.assertTrue(presence.is_current({"status": "restricted", "is_member": True}))
        for status in ("left", "kicked", "unknown", "restricted"):
            self.assertFalse(presence.is_current({"status": status}))

    def test_former_unknown_and_name_only_people_are_not_listed(self):
        users = [{"user_id": uid, "name": str(uid)} for uid in (1, 2, 3, None)]
        cached = {1: record(1), 2: record(2, "left"), 3: record(3, "unknown")}
        with patch.object(presence, "records", return_value=cached):
            people, coverage = presence.filter_current(CHAT, users, NOW, refresh=False)
        self.assertEqual([m["user_id"] for m in people], [1])
        self.assertEqual(coverage["former"], 1)
        self.assertEqual(coverage["unverified"], 2)

    def test_verification_budget_is_shared_and_unknown_is_not_assumed_left(self):
        users = [{"user_id": uid} for uid in range(1, 6)]
        budget = {"remaining": 2}
        with patch.object(presence, "records", return_value={}), \
                patch.object(presence, "_verify", side_effect=lambda chat, uid, now: record(uid)) as verify:
            people, coverage = presence.filter_current(CHAT, users, NOW, budget=budget)
        self.assertEqual(verify.call_count, 2)
        self.assertEqual(len(people), 2)
        self.assertEqual(coverage["unverified"], 3)
        self.assertEqual(coverage["pending_verification"], 3)
        self.assertEqual(coverage["former"], 0)
        self.assertEqual(budget["remaining"], 0)

    def test_daily_reports_wait_for_membership_sweep_instead_of_publishing_partial_lists(self):
        capacities = {"members": {str(CHAT): [{"user_id": 7}]},
                      "membership": {str(CHAT): {"pending_verification": 3}}}
        with patch.object(report_topics, "enabled", return_value=True), \
                patch.object(reports.automation_controls, "chat_enabled", return_value=True), \
                patch.object(reports, "load_capacity", return_value={}), \
                patch.object(reports, "current_capacities", return_value=capacities), \
                patch.object(reports, "_read") as read, \
                patch.object(reports, "enqueue_scheduled_action") as enqueue:
            self.assertEqual(reports._queue_reports(NOW, {TARGET, CHAT}), 0)
        read.assert_not_called()
        enqueue.assert_not_called()

    def test_membership_uses_matching_telegram_identity_and_bounded_read_timeout(self):
        with patch.object(telegram_sender, "telegram_call", return_value={"user": {"id": 7}, "status": "left"}) as read, \
                patch.object(presence, "_save"), patch.object(presence, "records", return_value={}):
            result = presence._verify(CHAT, 7, NOW)
        read.assert_called_once_with("getChatMember", {"chat_id": CHAT, "user_id": 7}, timeout=3)
        self.assertEqual(result["status"], "left")
        self.assertFalse(result["current"])
        with patch.object(telegram_sender, "telegram_call", return_value={"user": {"id": 99}, "status": "member"}), \
                patch.object(presence, "_save"), patch.object(presence, "records", return_value={}):
            self.assertEqual(presence._verify(CHAT, 7, NOW)["status"], "unknown")

    def test_failed_human_lookup_finishes_attempt_without_adding_or_repeatedly_refreshing_member(self):
        cached = {}
        def save(chat, payload, now):
            cached[payload["user_id"]] = payload
        users = [{"user_id": 7}]
        with patch.object(telegram_sender, "telegram_call", side_effect=RuntimeError("lookup unavailable")) as lookup, \
                patch.object(presence, "_save", side_effect=save), \
                patch.object(presence, "records", side_effect=lambda chat: cached):
            people, coverage = presence.filter_current(CHAT, users, NOW, humans_only=True)
            self.assertEqual(people, [])
            self.assertEqual(coverage["pending_verification"], 0)
            self.assertEqual(coverage["unverified"], 1)
            self.assertIsNone(cached[7]["is_bot"])
            presence.filter_current(CHAT, users, NOW + dt.timedelta(minutes=1), humans_only=True)
            self.assertEqual(lookup.call_count, 1)
            presence.filter_current(CHAT, users, NOW + dt.timedelta(minutes=11), humans_only=True)
            self.assertEqual(lookup.call_count, 2)

    def test_read_membership_works_for_paused_teams_without_enabling_sends(self):
        result = {"ok": True, "result": {"status": "left", "user": {"id": 7}}}
        with patch.dict(os.environ, {"AUTOMATION_CHAT_IDS": "", "TELEGRAM_BOT_TOKEN": "test-token"}), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(result).encode())) as network:
            self.assertEqual(telegram_sender.telegram_call("getChatMember", {"chat_id": CHAT, "user_id": 7})["status"], "left")
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.telegram_call("sendMessage", {"chat_id": CHAT, "text": "blocked"})
        self.assertEqual(network.call_count, 1)

    def test_departure_service_event_updates_cache_without_messaging(self):
        update = {"message": {"chat": {"id": CHAT}, "date": int(NOW.timestamp()), "left_chat_member": {"id": 7}}}
        with patch.object(presence, "records", return_value={}), patch.object(presence, "_save") as save:
            presence.observe(update, {CHAT})
        payload = save.call_args.args[1]
        self.assertEqual(payload["status"], "left")
        self.assertFalse(payload["current"])

    def test_cache_write_preserves_newer_observed_membership(self):
        with patch.object(presence, "request", side_effect=[[], None]) as write:
            presence._save(CHAT, record(7), NOW)
        path, payload = write.call_args.args
        self.assertIn("payload-%3E%3Everified_at=lte.", path)
        from postgres_archive import compile_request
        compile_request(path, payload, method="PATCH")

    def test_delivery_rejects_a_capacity_post_for_someone_who_just_left(self):
        action = {"chat_id": TARGET, "dedupe_key": "organized-daily:current", "payload": {
            "organized_report_version": reports.VERSION, "report_source_chat_id": CHAT, "capacity_member_ids": [7]}}
        with patch.dict(os.environ, {"ORGANIZED_REPORTS_ENABLED": "true"}), \
                patch.object(presence, "records", return_value={7: record(7, "left")}):
            self.assertFalse(report_topics.delivery_allowed(action))

    def test_members_endpoint_is_authenticated_and_only_reads_cache(self):
        env = {"INSIGHTS_API_KEY": "key", "ALLOWED_CHAT_IDS": f"{TARGET},{CHAT}"}
        with patch.dict(os.environ, env), patch.object(presence, "records", return_value={7: record(7)}) as read, \
                patch.object(presence, "_verify") as live:
            self.assertEqual(call_app("/api/reports/members")[0], 401)
            status, body = call_app("/api/reports/members", headers={"Authorization": "Bearer key"})
        self.assertEqual(status, 200)
        self.assertTrue(body["read_only"])
        read.assert_called_once_with(CHAT)
        live.assert_not_called()

    def test_delivery_audit_reads_newest_actions_only_for_an_approved_group(self):
        env = {"INSIGHTS_API_KEY": "key", "ALLOWED_CHAT_IDS": f"{TARGET},{CHAT}"}
        with patch.dict(os.environ, env), patch("app.list_scheduled_actions", return_value=[]) as read:
            status, _ = call_app("/api/schedules", query=f"group={TARGET}&newest=1&status=sent", headers={"Authorization": "Bearer key"})
            self.assertEqual(status, 200)
            read.assert_called_once_with({TARGET}, status="sent", limit=100, newest=True)
            self.assertEqual(call_app("/api/schedules", query="group=-1&newest=1", headers={"Authorization": "Bearer key"})[0], 403)
            self.assertEqual(call_app("/api/schedules", query="newest=invalid", headers={"Authorization": "Bearer key"})[0], 400)
        from cloud_store import list_scheduled_actions
        with patch("cloud_store.request", return_value=[]) as query:
            list_scheduled_actions({TARGET}, newest=True)
        self.assertIn("order=id.desc", query.call_args.args[0])


class DailySalaryTotalsTests(unittest.TestCase):
    def test_close_only_member_has_zero_recorded_earnings_even_with_unknown_team_rate(self):
        from decimal import Decimal
        entries = [{"kind": "dd", "count": 1, "gross": Decimal("1000"), "page": "test",
                    "row": {"author_id": 7, "author_name": "A"}},
                   {"kind": "cd", "count": 1, "gross": Decimal(0), "page": "test",
                    "row": {"author_id": 8, "author_name": "B"}}]
        with patch.object(reports, "messages_for_day", return_value=([], False)), \
                patch.object(reports.done_deal_summary, "summarize", return_value={"total": Decimal("1000"), "count": 1, "review_count": 0}), \
                patch("deal_parser.collect_deals", return_value={"entries": entries, "uncertain_rows": []}), \
                patch.object(reports, "payroll_count", return_value=None):
            finance = reports.finances(CHAT, DAY)
        workers = {w["user_id"]: w for w in finance["workers"]}
        self.assertIsNone(workers[7]["salary"])
        self.assertEqual(workers[8]["salary"], "0")

    def test_member_earnings_are_combined_across_teams_once_by_id(self):
        data = {CHAT: {"workers": [{"user_id": 7, "name": "Melo", "gross": "1000", "salary": "350"}]},
                SUNO: {"workers": [{"user_id": 7, "name": "Melo", "gross": "500", "salary": "100"}]}}
        text = reports.member_salary_totals(DAY, data)
        self.assertEqual(text.count("• Melo:"), 1)
        self.assertIn("Melo: ₱450 total for the day", text)
        self.assertIn("If VEO 35%: ₱450 · If VEO 40%: ₱500", text)
        self.assertIn(str(DAY), text)

    def test_missing_tier_shows_known_plus_conditional_range_not_a_guessed_total(self):
        data = {CHAT: {"workers": [{"user_id": 7, "name": "Melo", "gross": "1000", "salary": None}]},
                SUNO: {"workers": [{"user_id": 7, "name": "Melo", "gross": "500", "salary": "100"}]}}
        text = reports.member_salary_totals(DAY, data)
        self.assertIn("Melo: final pending", text)
        self.assertIn("If VEO 35%: ₱450 · If VEO 40%: ₱500", text)
        self.assertIn("Known: ₱100", text)
        self.assertNotIn("₱450 total for the day", text)

    def test_same_name_without_identity_is_not_merged_across_teams(self):
        worker = {"user_id": None, "name": "Alex", "gross": "100", "salary": "35"}
        text = reports.member_salary_totals(DAY, {CHAT: {"workers": [worker]}, SUNO: {"workers": [worker]}})
        self.assertIn("Alex (Veo): ₱35", text)
        self.assertIn("Alex (Trabawho?): ₱35", text)

    def test_both_scenarios_are_shown_even_when_forty_percent_is_confirmed(self):
        worker = {"user_id": 7, "name": "Melo", "gross": "1000", "salary": "400", "count": 2}
        finance = {"gross": "1000", "salary": "400", "net": "600", "count": 2,
            "workers": [worker], "commission_rate": "0.40", "quota_met": True,
            "salary_basis": "40% applies.", "partial": False, "review": []}
        text = reports.daily_sections(CHAT, DAY, finance, [], [], True)["salary"]
        self.assertIn("If 35%: ₱350 · If 40%: ₱400", text)
        self.assertIn("If 35%: salary ₱350 · Remaining ₱650", text)
        self.assertIn("If 40%: salary ₱400 · Remaining ₱600", text)
        self.assertIn("Applied: ₱1,000 × 40% = ₱400", text)
        totals = reports.member_salary_totals(DAY, {CHAT: finance})
        self.assertIn("Melo: ₱400 total for the day", totals)
        self.assertIn("If VEO 35%: ₱350 · If VEO 40%: ₱400", totals)

    def test_scenario_totals_round_each_member_and_keep_suno_written_earnings_fixed(self):
        from decimal import Decimal
        workers = [{"user_id": uid, "name": str(uid), "gross": "0.10", "salary": "0.04"} for uid in (1, 2, 3)]
        self.assertEqual(reports.commission_totals(workers), (Decimal("0.12"), Decimal("0.12")))
        veo = {"gross": "0.30", "salary": "0.12", "net": "0.18", "count": 3, "workers": workers}
        suno = {"gross": "500", "salary": "100", "net": "400", "count": 1, "workers": []}
        text = reports.summary_text(DAY, {CHAT: veo, SUNO: suno})
        self.assertIn("If VEO 35% + Suno earnings: salary ₱100.12 · Remaining ₱400.18", text)
        self.assertIn("If VEO 40% + Suno earnings: salary ₱100.12 · Remaining ₱400.18", text)

    def test_pending_team_rate_shows_two_labeled_options_without_selecting_a_salary(self):
        finance = {"gross": "1000", "salary": None, "net": None, "count": 1,
            "workers": [{"user_id": 7, "name": "Melo", "gross": "1000", "salary": None, "count": 1}],
            "salary_basis": "Quota unknown.", "partial": False, "review": []}
        text = reports.daily_sections(CHAT, DAY, finance, [], [], True)["salary"]
        self.assertIn("If 35%: ₱350 · If 40%: ₱400", text)
        self.assertIn("Final: pending quota review", text)
        self.assertIn("Total salary: pending", text)
        self.assertNotIn("Recorded salary:", text)

    def test_receipt_only_members_do_not_receive_veo_percentage_scenarios(self):
        worker = {"user_id": 7, "name": "Yang", "gross": "500", "salary": "100"}
        text = reports.member_salary_totals(DAY, {SUNO: {"workers": [worker]}})
        self.assertIn("Yang: ₱100 total for the day", text)
        self.assertNotIn("If VEO", text)

    def test_capacity_reports_show_actual_target_hits_without_a_success_guarantee(self):
        start = DAY - dt.timedelta(days=8)
        member = {"daily": [{"date": str(start + dt.timedelta(days=i)), "attendance": "Active", "done_deals": n}
            for i, n in enumerate([0, 1, 1, 2, 2, 2, 3, 3, 3])]}
        p = recent_capacity.planning(member, DAY)
        self.assertEqual(p["estimated_capacity_per_day"], 3)
        self.assertEqual(p["safe_plan_per_day"], 2)
        self.assertEqual(p["safe_target_met_active_days"], 6)
        self.assertEqual(p["safe_target_active_days"], 9)


if __name__ == "__main__":
    unittest.main()
