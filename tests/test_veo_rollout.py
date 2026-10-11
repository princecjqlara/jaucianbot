"""Exercise Ollie and Jessa's CRM routing and shared assignment workflows."""
import datetime as dt
import json
import unittest
from unittest.mock import patch

import client_volunteers as volunteers
import daily_automation as daily
import freebie_automation as freebies
import new_client_automation as clients
from test_hourly_assignments import offer
from test_new_client_automation import assignment, contact
from test_wsgi import call_app


NOW = dt.datetime(2026, 10, 9, 1, tzinfo=dt.timezone.utc)  # 9 AM PHT
TEAMS = {
    -1004423057797: (5758, {"Loki Media", "Vista Media Agency"}),
    -1003962888977: (3725, {"Manawari Studios"}),
}
MEMBERS = [
    {"user_id": 11, "user_name": "Alex", "available_hours": [9]},
    {"user_id": 22, "user_name": "Bea", "available_hours": [9]},
    {"user_id": 33, "user_name": "Later", "available_hours": [10]},
]


def team_offer(chat_id, *, volunteer=False):
    row = offer() if volunteer else assignment(
        100, 11, "open", 1, status="pending", assigned_at=NOW,
    )
    row["chat_id"] = chat_id
    row["payload"].update(
        new_client_thread_id=TEAMS[chat_id][0],
        new_client_work_date=NOW.date().isoformat(),
        new_client_page=next(iter(daily.GROUPS[chat_id]["crm_pages"])),
    )
    return row


class VeoRolloutTests(unittest.TestCase):
    def setUp(self):
        for target, value in (
            ("new_client_automation.new_client_reply_messages", []),
            ("new_client_automation.contact_identity_map", {}),
        ):
            mocked = patch(target, return_value=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_completed_briefs_stay_in_their_team_and_current_selected_hour(self):
        pages = {
            page_id: page
            for chat in TEAMS for page, page_id in daily.GROUPS[chat]["crm_pages"].items()
        }

        def read(page_id):
            row = contact(page_id, "2026-10-01")
            row["page_id"] = page_id
            return [row]

        with patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "true"}), patch.object(
            clients, "new_client_actions", return_value=[]
        ), patch.object(clients, "_active_members", return_value=MEMBERS), patch.object(
            clients, "completed_detail_contacts", side_effect=read
        ) as crm, patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(clients.queue_new_client_assignments(NOW, set(TEAMS)), 3)
        self.assertEqual({call.args[0] for call in crm.call_args_list}, set(pages))
        for call in enqueue.call_args_list:
            chat, payload = call.kwargs["chat_id"], call.kwargs["payload"]
            thread, team_pages = TEAMS[chat]
            self.assertEqual(payload["message_thread_id"], thread)
            self.assertEqual(payload["new_client_thread_id"], thread)
            self.assertIn(payload["new_client_page"], team_pages)
            self.assertIn(payload["new_client_contact_page_id"], daily.GROUPS[chat]["crm_pages"].values())
            self.assertIn(payload["new_client_assignee_id"], {11, 22})
            self.assertEqual(payload["new_client_response_minutes"], 20)
            self.assertEqual(payload["new_client_volunteer_minutes"], 10)
            self.assertEqual(payload["new_client_direct_timeout_minutes"], 20)
            self.assertIn(f"Client token: {payload['new_client_token']}", payload["text"])

    def test_ready_editor_keeps_receiving_when_peer_has_an_open_offer_in_fallback(self):
        for chat in TEAMS:
            with self.subTest(chat=chat):
                open_offer = team_offer(chat)
                previous = team_offer(chat)
                previous["id"] = 99
                previous["status"] = "cancelled"
                previous["payload"].update(new_client_assignee_id=22,
                    new_client_contact_id="previous", new_client_contact_name="Previous client",
                    new_client_acknowledged_at=NOW.isoformat())
                with patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "false"}), patch.object(
                    clients, "new_client_actions", return_value=[previous, open_offer]
                ), patch.object(clients, "_active_members", return_value=MEMBERS), patch.object(
                    clients, "_page_contacts", return_value=[(previous["payload"]["new_client_page"], [contact("fresh", "2026-10-01")])]
                ), patch.object(clients, "enqueue_scheduled_action", return_value=True) as enqueue:
                    self.assertEqual(clients.queue_new_client_assignments(NOW, {chat}), 1)
                self.assertEqual(enqueue.call_args.kwargs["payload"]["new_client_assignee_id"], 22)
                self.assertEqual(enqueue.call_args.kwargs["repeat_interval_minutes"], 30)

    def test_fallback_rescues_unconfirmed_offer_after_30_minutes_in_both_teams(self):
        for chat in TEAMS:
            with self.subTest(chat=chat):
                row = team_offer(chat)
                at = NOW + dt.timedelta(minutes=30)
                with patch.dict("os.environ", {"NEW_CLIENT_VOLUNTEER_ENABLED": "false"}), patch.object(
                    clients, "_active_members", return_value=MEMBERS
                ), patch.object(clients, "update_new_client_action", return_value=True) as update:
                    clients._release_stale_assignments([row], at - dt.timedelta(seconds=1))
                    update.assert_not_called()
                    clients._release_stale_assignments([row], at)
                self.assertEqual(update.call_args.args[1]["new_client_cancelled_reason"], clients.REASSIGN_REASON_30)

    def test_volunteer_invitation_and_claim_use_each_teams_new_client_topic(self):
        for chat, (thread, _) in TEAMS.items():
            with self.subTest(chat=chat):
                row = team_offer(chat, volunteer=True)
                at = NOW + dt.timedelta(minutes=20)
                with patch.object(clients, "_active_members", return_value=MEMBERS), patch.object(
                    volunteers, "enqueue_scheduled_action", return_value=True
                ) as enqueue, patch.object(volunteers, "update_new_client_action", return_value=True):
                    volunteers.release(row, at)
                self.assertEqual(row["payload"]["new_client_phase"], "volunteer")
                notice = enqueue.call_args.kwargs
                self.assertEqual(notice["chat_id"], chat)
                self.assertEqual(notice["payload"]["message_thread_id"], thread)
                self.assertIn("tg://user?id=22", notice["payload"]["text"])
                self.assertNotIn("tg://user?id=33", notice["payload"]["text"])
                reply = {"from": {"id": 22}, "message_thread_id": thread,
                         "date": int((at + dt.timedelta(minutes=1)).timestamp()),
                         "text": "TAKE AABBCCDD"}
                with patch.object(volunteers, "start", return_value=at), patch.object(
                    clients, "_active_members", return_value=MEMBERS
                ):
                    self.assertTrue(clients._confirms_assignment(reply, row))
                    reply["message_thread_id"] = thread + 1
                    self.assertFalse(clients._confirms_assignment(reply, row))
                    reply["message_thread_id"] = thread
                    reply["from"]["id"] = 33
                    self.assertFalse(clients._confirms_assignment(reply, row))

    def test_paid_freebies_use_the_same_pages_and_the_separate_freebie_topics(self):
        def read(page_id):
            return [{"id": page_id, "name": "Paid client", "last_interaction_at": "2026-10-01"}]

        with patch.object(freebies, "freebie_actions", return_value=[]), patch.object(
            freebies, "_active_members", return_value=MEMBERS
        ), patch.object(freebies, "paid_contacts", side_effect=read) as crm, patch.object(
            freebies, "_queue_no_contact_notice", return_value=False
        ), patch.object(freebies, "enqueue_scheduled_action", return_value=True) as enqueue:
            self.assertEqual(freebies.queue_freebie_assignments(NOW, set(TEAMS)), 3)
        self.assertEqual({call.args[0] for call in crm.call_args_list}, {
            page_id for chat in TEAMS for page_id in daily.GROUPS[chat]["crm_pages"].values()
        })
        for call in enqueue.call_args_list:
            chat, payload = call.kwargs["chat_id"], call.kwargs["payload"]
            self.assertEqual(payload["message_thread_id"], daily.GROUPS[chat]["freebie"])
            self.assertNotEqual(payload["message_thread_id"], TEAMS[chat][0])
            self.assertIn(payload["freebie_page"], TEAMS[chat][1])
            self.assertIn(payload["freebie_assignee_id"], {11, 22})

    def test_quota_reminders_reach_both_teams_announcement_topics(self):
        progress = {"dd_total": 0, "cd_total": 0, "capped": False, "uncertain": False}
        with patch.object(daily, "existing_daily_reminder_chats", return_value=set()), patch.object(
            daily, "daily_poll_counts", return_value={}
        ), patch.object(daily, "deal_progress_for_day", return_value=progress), patch.object(
            daily, "enqueue_scheduled_action", return_value=True
        ) as enqueue:
            self.assertEqual(daily.queue_daily_reminders(NOW.date(), 7, NOW, set(TEAMS)), 2)
        self.assertEqual({call.kwargs["chat_id"] for call in enqueue.call_args_list}, set(TEAMS))
        for call in enqueue.call_args_list:
            self.assertEqual(call.kwargs["payload"]["message_thread_id"],
                             daily.GROUPS[call.kwargs["chat_id"]]["announcements"])

    def test_today_poll_webhook_triggers_both_assignment_workflows_for_each_team(self):
        update = {"poll_answer": {"poll_id": "today", "user": {"id": 11}, "option_ids": [0]}}
        for chat in TEAMS:
            with self.subTest(chat=chat), patch.dict("os.environ", {
                "ALLOWED_CHAT_IDS": str(chat), "CRM_SUPABASE_SERVICE_ROLE_KEY": "test",
                "TELEGRAM_WEBHOOK_SECRET": "test",
            }, clear=True), patch("app.save_poll_answer", return_value=True), patch(
                "app.poll_answer_chat_today", return_value=chat
            ), patch("app.queue_freebie_assignments", return_value=1) as freebie_queue, patch(
                "app.queue_new_client_assignments", return_value=1
            ) as client_queue, patch("app.deliver_due_actions", return_value=(2, 2, 0)) as deliver:
                status, _ = call_app("/api/webhook", method="POST",
                    headers={"X-Telegram-Bot-Api-Secret-Token": "test"}, body=json.dumps(update).encode())
            self.assertEqual(status, 200)
            self.assertEqual(client_queue.call_args.args[1], {chat})
            self.assertEqual(freebie_queue.call_args.args[1], {chat})
            deliver.assert_called_once_with({chat}, limit=5)

    def test_daily_client_report_lists_each_configured_page_and_confirmation(self):
        for chat, (_, pages) in TEAMS.items():
            with self.subTest(chat=chat):
                row = team_offer(chat)
                row["payload"]["new_client_acknowledged_at"] = NOW.isoformat()
                with patch.object(clients, "new_client_actions", return_value=[row]) as history:
                    lines = "\n".join(clients.new_client_report_lines(chat, NOW.date()))
                history.assert_called_once_with({chat})
                self.assertIn("Team total: 1", lines)
                for page in pages:
                    self.assertIn(page, lines)
                self.assertIn("User 11: 1", lines)
