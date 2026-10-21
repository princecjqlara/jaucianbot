import datetime as dt
import json
import os
import unittest
from unittest.mock import patch

import app
import automation_controls as controls
import client_workflow as workflow
import cloud_store
import daily_automation as daily
import new_client_automation as clients
import page_access
import ready_availability as ready
import telegram_sender
from test_wsgi import call_app

MAIN = -1003647732254
MAKATA = daily.MAKATA_CHAT_ID
NOW = dt.datetime(2026, 10, 21, 19, 10, tzinfo=dt.timezone.utc)
ENV = {"VEO_NEW_CLIENTS_ENABLED": "false", "NEW_CLIENT_READY_ENABLED": "true"}


class VeoFeatureSwitchTests(unittest.TestCase):
    def test_all_veo_teams_can_run_polls_and_reports_without_new_clients(self):
        active = set(daily.GROUPS) | {daily.TRABAWHO_CHAT_ID, MAKATA, daily.DAILY_REPORTS_CHAT_ID}
        with patch.dict(os.environ, {**ENV, "AUTOMATION_CHAT_IDS": ",".join(map(str, active))}):
            self.assertEqual(controls.enabled_chat_ids(active), active)
            for chat in daily.GROUPS:
                self.assertFalse(controls.new_clients_enabled(chat))
                self.assertFalse(ready.enabled(chat))
            self.assertTrue(controls.new_clients_enabled(MAKATA))
            self.assertTrue(controls.new_clients_enabled(daily.TRABAWHO_CHAT_ID))
            state = controls.status(active)
        self.assertEqual(set(state["new_client_disabled_chat_ids"]), set(daily.GROUPS))
        self.assertNotIn(daily.DAILY_REPORTS_CHAT_ID, state["new_client_enabled_chat_ids"])

    def test_disabled_veo_queue_exits_before_reading_or_changing_ownership(self):
        with patch.dict(os.environ, ENV), patch.object(clients, "new_client_actions") as history:
            self.assertEqual(clients.queue_new_client_assignments(NOW, set(daily.GROUPS)), 0)
            self.assertFalse(clients.confirm_new_client_reply({"message": {"chat": {"id": MAIN}, "text": "WORKING AABBCCDD"}}, {MAIN}))
        history.assert_not_called()

    def test_enqueue_and_sender_block_old_offers_but_allow_availability_and_reports(self):
        action = {"chat_id": MAIN, "action_type": "message", "dedupe_key": "new-client:old",
                  "payload": {"new_client_token": "AABBCCDD", "text": "Old offer"}}
        with patch.dict(os.environ, ENV), patch.object(cloud_store, "request", return_value=[{"id": 1}]) as db:
            self.assertFalse(cloud_store.enqueue_scheduled_action(chat_id=MAIN, action_type="message",
                dedupe_key=action["dedupe_key"], payload=action["payload"], scheduled_for=NOW))
            db.assert_not_called()
            with self.assertRaises(telegram_sender.TelegramError):
                telegram_sender.send_scheduled_action(action)
            self.assertTrue(cloud_store.enqueue_scheduled_action(chat_id=MAIN, action_type="poll", scheduled_for=NOW,
                dedupe_key="daily-poll:today", payload={"question": "Active today?", "options": ["Yes", "No"]}))
        self.assertTrue(controls.new_client_delivery_allowed({"chat_id": daily.DAILY_REPORTS_CHAT_ID, "payload": action["payload"]}))

    def test_already_queued_offer_is_deferred_without_claiming_or_releasing_the_client(self):
        action = {"id": 7, "chat_id": MAIN, "status": "processing", "payload": {"new_client_token": "AABBCCDD"}}
        with patch.dict(os.environ, {**ENV, "AUTOMATION_CHAT_IDS": str(MAIN)}), \
                patch.object(app, "defer_scheduled_action", return_value=True) as defer, \
                patch.object(app, "send_scheduled_action") as send, patch.object(app, "finish_scheduled_action") as finish:
            self.assertEqual(app._deliver_claimed_actions([action], {MAIN}), (1, 0, 0))
        defer.assert_called_once_with(action)
        send.assert_not_called()
        finish.assert_not_called()

    def test_old_acceptance_button_does_not_change_ownership(self):
        update = {"callback_query": {"id": "cb", "from": {"id": 7}, "data": "cw:accept:AABBCCDD",
            "message": {"chat": {"id": MAIN}, "message_thread_id": daily.GROUPS[MAIN]["contact_thread"]}}}
        with patch.dict(os.environ, ENV), patch.object(workflow, "history_for") as read:
            self.assertEqual(workflow.handle(update, {MAIN}, NOW), controls.NEW_CLIENTS_OFF)
        read.assert_not_called()

    def test_maintenance_skips_disabled_veo_and_main_attendance_covers_all_members(self):
        users = [{"user_id": 1}, {"user_id": 2}]
        with patch.dict(os.environ, ENV), patch.object(workflow, "history_for") as history, \
                patch.object(page_access, "queue_poll") as pages, patch.object(page_access, "members") as access:
            workflow.maintain(NOW, set(daily.GROUPS))
            self.assertEqual(page_access.scoped_voters(MAIN, users, dt.date(2026, 10, 22)), users)
        history.assert_not_called()
        pages.assert_not_called()
        access.assert_not_called()

    def test_veo_yes_no_polls_remain_enabled_without_sama_only_scope(self):
        with patch.dict(os.environ, ENV), patch.object(daily, "enqueue_scheduled_action", return_value=True) as queue:
            self.assertEqual(daily.queue_daily_polls(dt.date(2026, 10, 23), NOW, set(daily.GROUPS)), 6)
        for call in queue.call_args_list:
            self.assertEqual(call.kwargs["payload"]["options"], ["Yes", "No"])
            self.assertNotIn("Sama Ka Na Media", call.kwargs["payload"]["question"])


class MakataStartupTests(unittest.TestCase):
    def test_ready_button_queues_a_check_without_sending_from_the_webhook(self):
        update = {"update_id": 10, "callback_query": {"id": "cb", "data": "cw:ready",
            "message": {"chat": {"id": MAKATA}, "message_thread_id": 13}}}
        env = {**ENV, "AUTOMATION_CHAT_IDS": str(MAKATA), "ALLOWED_CHAT_IDS": str(MAKATA), "TELEGRAM_WEBHOOK_SECRET": "secret"}
        with patch.dict(os.environ, env), patch.object(app.makata_automation, "hydrate"), \
                patch.object(app.makata_automation, "register_private", return_value=False), \
                patch.object(workflow, "handle", return_value="Availability updated."), \
                patch.object(app, "queue_new_client_assignments", return_value=1) as queue, \
                patch.object(app, "deliver_due_actions") as deliver:
            status, _ = call_app("/api/webhook", method="POST", body=json.dumps(update).encode(),
                headers={"X-Telegram-Bot-Api-Secret-Token": "secret"})
        self.assertEqual(status, 200)
        self.assertEqual(queue.call_args.args[1], {MAKATA})
        deliver.assert_not_called()

    def test_makata_directory_explains_no_ready_members_and_retains_start_buttons(self):
        contacts = [("Makata Studio", [{"id": "c1", "name": "Client", "page_id": "p1"}])]
        with patch.dict(daily.AVAILABILITY_GROUPS, {MAKATA: daily.MAKATA}), \
                patch.object(workflow, "history_for", return_value=[]), patch.object(cloud_store, "allowed_chat_ids", return_value={MAKATA}), \
                patch.object(clients, "_page_contacts", return_value=contacts), \
                patch.object(ready, "available", return_value=[]), patch.object(page_access, "page_map", return_value={"Makata Studio": "p1"}), \
                patch.object(page_access, "members", return_value={}):
            snapshot = workflow.directory(MAKATA, NOW)
        text, buttons = workflow.render_directory(snapshot)
        self.assertIn("Waiting for Ready members", text)
        self.assertIn("60 minutes", text)
        self.assertIn("cw:ready", str(buttons))
        self.assertIn("cw:pages", str(buttons))


if __name__ == "__main__":
    unittest.main()
