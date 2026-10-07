import datetime as dt
import json
import unittest
from unittest.mock import patch

from daily_automation import AVAILABILITY_GROUPS, GROUPS, TRABAWHO, TRABAWHO_CHAT_ID, queue_daily_polls
from new_client_automation import _enrich_history_contact_identities, _page_contacts, queue_new_client_assignments
from trabawho_automation import (
    availability_checkin_text, plan_text, plan_totals, progress_text,
    queue_trabawho_automation, trabawho_poll_work_date,
)
from test_wsgi import call_app


CHAT = TRABAWHO_CHAT_ID
NOW = dt.datetime(2026, 10, 8, 2, tzinfo=dt.timezone.utc)
TODAY = dt.date(2026, 10, 8)
TOMORROW = TODAY + dt.timedelta(days=1)


class TrabawhoAutomationTests(unittest.TestCase):
    def setUp(self):
        # Each workflow has its own tests; these tests isolate availability/Suno.
        for name in ("queue_song_followups", "queue_trabawho_daily_report"):
            mock = patch("app." + name, return_value=0)
            mock.start()
            self.addCleanup(mock.stop)
        followups = patch("trabawho_automation.queue_trabawho_followups", return_value=0)
        followups.start()
        self.addCleanup(followups.stop)

    def test_topics_and_group_workflows_are_separate_from_veo_sales_and_freebies(self):
        self.assertEqual((TRABAWHO["general"], TRABAWHO["announcements"], TRABAWHO["active"], TRABAWHO["contact_thread"]), (1, 16, 7581, 7673))
        self.assertIn(CHAT, AVAILABILITY_GROUPS)
        self.assertNotIn(CHAT, GROUPS)
        self.assertNotIn("freebie", TRABAWHO)
        self.assertNotIn("done", TRABAWHO)

    def test_user_formulas_and_zero_active_members(self):
        self.assertEqual(plan_totals(10), (20, 3000))
        self.assertEqual(plan_totals(1), (2, 300))
        self.assertEqual(plan_totals(0), (0, 0))
        with self.assertRaises(ValueError):
            plan_totals(-1)
        text = plan_text(TOMORROW, 10, TODAY)
        self.assertIn("TRABAWHO TEAM UPDATE — TOMORROW", text)
        self.assertIn("10 teammates marked Active", text)
        self.assertNotIn("Team quota", text)
        self.assertNotIn("Ads budget", text)
        self.assertNotIn("× 2", text)
        self.assertNotIn("commission", text.lower())

    def test_poll_is_dated_nonanonymous_and_uses_active_topic(self):
        with patch("daily_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(queue_daily_polls(TOMORROW, NOW, {CHAT}), 1)
        action = enqueue.call_args.kwargs
        self.assertEqual(action["payload"]["message_thread_id"], 7581)
        self.assertEqual(action["payload"]["daily_poll_date"], "2026-10-09")
        self.assertFalse(action["payload"]["is_anonymous"])

    def test_no_poll_creates_polls_without_guessing_quota_or_budget(self):
        with patch("trabawho_automation.daily_poll_counts", return_value={}), patch(
            "trabawho_automation.queue_daily_polls", return_value=1
        ) as polls, patch("trabawho_automation.enqueue_scheduled_action") as enqueue:
            self.assertEqual(queue_trabawho_automation(NOW, {CHAT}), 2)
        self.assertEqual(polls.call_count, 2)
        enqueue.assert_not_called()

    def test_changed_active_count_updates_announcements_and_unchanged_count_is_deduped(self):
        with patch("trabawho_automation.daily_poll_counts", return_value={CHAT: {"active_workers": 10}}), patch(
            "trabawho_automation.request", side_effect=[[{"count": 9}], [{"count": 10, "format": "friendly-v1"}]]
        ), patch("trabawho_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(queue_trabawho_automation(NOW, {CHAT}), 1)
        payload = enqueue.call_args.kwargs["payload"]
        self.assertEqual(payload["message_thread_id"], 16)
        self.assertIn("10 teammates marked Active", payload["text"])
        self.assertNotIn("₱", payload["text"])
        self.assertNotIn("× 2", payload["text"])
        self.assertEqual(payload["trabawho_work_date"], TODAY.isoformat())

    def test_old_plan_format_is_requeued_once_for_friendly_migration(self):
        with patch("trabawho_automation.daily_poll_counts", return_value={CHAT: {"active_workers": 10}}), patch(
            "trabawho_automation.request", return_value=[{"count": 10}]
        ), patch("trabawho_automation.enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(queue_trabawho_automation(NOW, {CHAT}), 2)
        self.assertEqual(enqueue.call_args.kwargs["payload"]["trabawho_plan_format"], "friendly-v1")

    def test_checkin_is_gentle_and_mentions_active_and_not_active_voters(self):
        text = availability_checkin_text(TODAY, [
            {"user_id": 11, "user_name": "Alex", "active": True},
            {"user_id": 12, "user_name": "Bea", "active": False},
        ], {})
        self.assertIn("Alex", text)
        self.assertIn("Bea", text)
        self.assertIn("marked Active today", text)
        self.assertIn("marked Not Active today", text)
        self.assertIn("If you haven't voted yet", text)
        self.assertIn("no pressure", text)

    def test_progress_shows_remaining_clients_without_internal_formula(self):
        text = progress_text(TODAY, {"active_workers": 1}, 1)
        self.assertIn("1 of 2 planned", text)
        self.assertIn("1 left to go", text)
        self.assertNotIn("× 2", text)
        self.assertNotIn("₱", text)

    def test_unapproved_group_never_queues_actions(self):
        with patch("trabawho_automation.daily_poll_counts") as counts:
            self.assertEqual(queue_trabawho_automation(NOW, set()), 0)
        counts.assert_not_called()

    def test_tomorrow_poll_resolves_to_its_work_date(self):
        with patch("trabawho_automation.daily_poll_counts", side_effect=[
            {CHAT: {"poll_id": "today"}}, {CHAT: {"poll_id": "tomorrow"}},
        ]):
            self.assertEqual(trabawho_poll_work_date("tomorrow", NOW, {CHAT}), TOMORROW)

    def test_suno_reader_does_not_use_veo_crm_or_legacy_identity_lookup(self):
        with patch("suno_store.completed_suno_contacts", return_value=[{"id": "suno-1"}]), patch(
            "new_client_automation.completed_detail_contacts"
        ) as crm:
            self.assertEqual(_page_contacts(TRABAWHO), [("Suno", [{"id": "suno-1"}])])
        crm.assert_not_called()

    def test_suno_assignments_use_the_relation_backed_page_name(self):
        with patch("suno_store.completed_suno_contacts", return_value=[
            {"id": "suno-1", "page_name": "Maico Foods"},
            {"id": "suno-2", "page_name": "Maico Foods"},
        ]):
            self.assertEqual(_page_contacts(TRABAWHO), [("Maico Foods", [
                {"id": "suno-1", "page_name": "Maico Foods"},
                {"id": "suno-2", "page_name": "Maico Foods"},
            ])])
        with patch("new_client_automation.contact_identity_map") as lookup:
            _enrich_history_contact_identities([{"chat_id": CHAT, "payload": {
                "new_client_token": "ABCDEF12", "new_client_contact_id": "suno-1",
            }}])
        lookup.assert_not_called()

    def test_completed_suno_contact_is_offered_in_new_client_topic_to_todays_active_member(self):
        contact = {"id": "suno-1", "page_id": "suno:pnhzpeyzpwsmwcuafgpw", "psid": "customer-1",
                   "name": "Client", "collected_details": {"song": "Birthday"}}
        with patch("new_client_automation.new_client_actions", return_value=[]), patch(
            "new_client_automation._active_members", return_value=[{"user_id": 11, "user_name": "Alex"}]
        ) as active, patch("suno_store.completed_suno_contacts", return_value=[contact]), patch(
            "new_client_automation.enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(queue_new_client_assignments(NOW, {CHAT}), 1)
        active.assert_called_once_with(CHAT, TODAY)
        action = enqueue.call_args.kwargs
        self.assertEqual(action["chat_id"], CHAT)
        self.assertEqual(action["payload"]["message_thread_id"], 7673)
        self.assertEqual(action["payload"]["new_client_work_date"], TODAY.isoformat())
        self.assertIn("Birthday", action["payload"]["text"])
        self.assertIn("WORKING", action["payload"]["text"])

    def test_todays_active_vote_assigns_suno_without_veo_credentials(self):
        update = {"poll_answer": {"poll_id": "today", "user": {"id": 11}, "option_ids": [0]}}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch(
            "app.save_poll_answer", return_value=True
        ), patch("app.trabawho_poll_work_date", return_value=TODAY), patch("app.dt") as clock, patch(
            "app.queue_trabawho_automation", return_value=1
        ), patch("app.suno_configured", return_value=True), patch(
            "app.queue_new_client_assignments", return_value=1
        ) as clients, patch("app.deliver_due_actions", return_value=(2, 2, 0)):
            clock.datetime.now.return_value = NOW
            clock.timezone.utc = dt.timezone.utc
            status, _ = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"}, body=json.dumps(update).encode())
        self.assertEqual(status, 200)
        clients.assert_called_once_with(NOW, {CHAT})

    def test_suno_dispatch_works_without_veo_credentials_and_failure_does_not_stop_deliveries(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "CRM_SUPABASE_SERVICE_ROLE_KEY": ""}, clear=True), patch(
            "app.run_due_daily_automation", return_value=0
        ), patch("app.queue_trabawho_automation", return_value=0), patch("app.suno_configured", return_value=True), patch(
            "app.claim_automation_slot", return_value=True
        ), patch("app.queue_new_client_assignments", side_effect=RuntimeError("source unavailable")), patch(
            "app.queue_freebie_assignments"
        ) as freebies, patch("app.deliver_due_actions", return_value=(1, 1, 0)) as deliver:
            status, body = call_app("/api/cron/dispatch")
        self.assertEqual(status, 503)
        self.assertEqual(body["queue_errors"], ["suno_new_client"])
        self.assertEqual(body["sent"], 1)
        freebies.assert_not_called()
        deliver.assert_called_once()

    def test_tomorrow_vote_updates_budget_without_assigning_clients_a_day_early(self):
        update = {"poll_answer": {"poll_id": "tomorrow", "user": {"id": 11}, "option_ids": [0]}}
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "TELEGRAM_WEBHOOK_SECRET": "test"}, clear=True), patch(
            "app.save_poll_answer", return_value=True
        ), patch("app.trabawho_poll_work_date", return_value=TOMORROW), patch(
            "app.dt"
        ) as clock, patch("app.queue_trabawho_automation", return_value=1), patch(
            "app.suno_configured", return_value=True
        ), patch("app.queue_new_client_assignments") as clients, patch(
            "app.deliver_due_actions", return_value=(1, 1, 0)
        ) as deliver:
            clock.datetime.now.return_value = NOW
            clock.timezone.utc = dt.timezone.utc
            status, _ = call_app("/api/webhook", method="POST", headers={"X-Telegram-Bot-Api-Secret-Token": "test"}, body=json.dumps(update).encode())
        self.assertEqual(status, 200)
        clients.assert_not_called()
        deliver.assert_called_once_with({CHAT}, limit=5)

    def test_missing_suno_mapping_does_not_hide_other_teams_status(self):
        with patch.dict("os.environ", {"ALLOWED_CHAT_IDS": str(CHAT), "INSIGHTS_API_KEY": "test"}, clear=True), patch(
            "app.new_client_status", return_value=[]
        ), patch("app.suno_configured", return_value=False):
            status, body = call_app("/api/new-clients/status", headers={"Authorization": "Bearer test"})
        self.assertEqual(status, 200)
        self.assertFalse(body["groups"][0]["configured"])


if __name__ == "__main__":
    unittest.main()
