import datetime as dt
import unittest
from unittest.mock import patch

import client_workflow
import daily_automation as daily
import freebie_automation as freebies
import hourly_availability as hourly
import new_client_automation as clients
import ready_availability as ready
from page_access_fixture import granted_access

LYN = -1004364797420
OLLIE = ready.OLLIE
NOW = dt.datetime(2026, 10, 14, 4, tzinfo=dt.timezone.utc)
PERSON = {"user_id": 11, "user_name": "Alex"}


class PendingPageSetupTests(unittest.TestCase):
    def setUp(self):
        granted_access(self)
        # Preserve the unconnected-page safeguard after Lyn's pages go live.
        config = patch.dict(daily.GROUPS[LYN], {"crm_pages": {}, "pages": {}})
        config.start()
        self.addCleanup(config.stop)

    def test_availability_and_reminders_use_lyn_topics_without_page_mapping(self):
        with patch.dict("os.environ", {"HOURLY_AVAILABILITY_START_DATE": "2026-10-09"}), patch.object(hourly, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(hourly.queue_polls(NOW.date(), NOW, daily.AVAILABILITY_GROUPS, {LYN}), 3)
        for call in queue.call_args_list:
            self.assertEqual(call.kwargs["chat_id"], LYN)
            self.assertEqual(call.kwargs["payload"]["message_thread_id"], 16)
        with patch.object(daily, "existing_daily_reminder_chats", return_value=set()), patch.object(daily, "daily_poll_counts", return_value={}), patch.object(daily, "deal_progress_for_day", return_value={
                "dd_total": 0, "cd_total": 0, "capped": False, "uncertain": False}), patch.object(daily, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(daily.queue_daily_reminders(NOW.date(), 13, NOW, {LYN}), 1)
        self.assertEqual(queue.call_args.kwargs["payload"]["message_thread_id"], 18)

    def test_pending_sources_do_not_create_empty_thread_pools_or_read_other_pages(self):
        with patch.object(clients, "completed_detail_contacts") as crm, patch.object(freebies, "paid_contacts") as paid:
            self.assertEqual(clients._page_contacts(daily.GROUPS[LYN]), [])
            self.assertEqual(freebies._page_contacts(daily.GROUPS[LYN]), [])
        crm.assert_not_called()
        paid.assert_not_called()

    def test_pending_page_does_not_send_freebies_or_empty_supply_notices(self):
        with patch.object(freebies, "freebie_actions", return_value=[]), patch.object(freebies, "_active_members", return_value=[PERSON]), patch.object(freebies, "_page_contacts") as read, patch.object(freebies, "enqueue_scheduled_action") as queue:
            self.assertEqual(freebies.queue_freebie_assignments(NOW, {LYN}), 0)
        read.assert_not_called()
        queue.assert_not_called()

    def test_lyn_onboarding_does_not_block_mapped_team_client_assignments(self):
        contact = {"id": "new-client", "name": "New client", "collected_details": {"request": "video"}}
        with patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "true"}), patch.object(clients, "new_client_actions", return_value=[]), patch.object(ready, "queue_instructions", return_value=0) as guide, patch.object(ready, "available", return_value=[PERSON]), patch.object(ready, "reserve", return_value=True), patch.object(clients, "_page_contacts", return_value=[("Loki Media", [contact])]) as read, patch.object(clients, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, {LYN, OLLIE}), 1)
        self.assertEqual({call.args[0] for call in guide.call_args_list}, {LYN, OLLIE})
        read.assert_called_once_with(daily.GROUPS[OLLIE])
        self.assertEqual(queue.call_args.kwargs["chat_id"], OLLIE)

    def test_status_distinguishes_pending_setup_from_no_available_clients(self):
        with patch.object(clients, "new_client_actions", return_value=[]), patch.object(clients, "_active_members", return_value=[PERSON]), patch.dict("os.environ", {"NEW_CLIENT_READY_ENABLED": "false"}):
            status = clients.new_client_status({LYN}, NOW)[0]
        self.assertFalse(status["configured"])
        self.assertTrue(status["page_mapping_pending"])
        self.assertEqual(status["members"][0]["state"], "waiting_for_page_mapping")

    def test_client_list_and_guide_explain_pending_page_with_working_controls(self):
        with patch.object(client_workflow, "history_for", return_value=[]):
            snapshot = client_workflow.directory(LYN, NOW)
        text, controls = client_workflow.render_directory(snapshot)
        self.assertTrue(snapshot["page_mapping_pending"])
        self.assertIn("Page setup pending", text)
        self.assertIn("paused", text)
        self.assertTrue(controls["inline_keyboard"])
        guide = client_workflow.guide(LYN)
        self.assertIn("PAGE SETUP PENDING", guide)
        self.assertIn("/clients", guide)
        self.assertNotIn("Hand over", guide)


if __name__ == "__main__":
    unittest.main()
