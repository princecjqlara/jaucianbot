import copy
import unittest
from unittest.mock import patch

import client_workflow as workflow
import new_client_automation as clients
import ready_availability as ready
import telegram_sender as sender
from daily_automation import GROUPS, TRABAWHO_CHAT_ID
from test_client_workflow import assignment, callback, NOW


class VeoHandoversDisabledTests(unittest.TestCase):
    def test_all_veo_commands_and_old_buttons_block_transfers_before_reads(self):
        for chat in GROUPS:
            commands = [callback(data, chat=chat) for data in
                        ("choose:100:0:0", "pick:100:0:22", "yes:100:1", "no:100:1", "cancel:100:1")]
            commands += [{"message": {"message_id": 600, "chat": {"id": chat}, "from": {"id": 11},
                           "message_thread_id": GROUPS[chat]["contact_thread"], "text": f"/{cmd} AAAABBBB"}}
                         for cmd in ("handover", "accept_handover", "decline_handover", "cancel_handover")]
            with self.subTest(chat=chat), patch.object(workflow, "full_assignment") as read, \
                    patch.object(workflow, "history_for") as history, patch.object(workflow, "queue_message"):
                for incoming in commands:
                    self.assertEqual(workflow.handle(incoming, {chat}, NOW), workflow.HANDOVER_DISABLED)
                read.assert_not_called()
                history.assert_not_called()

    def test_pending_veo_request_cannot_change_owner_through_helpers(self):
        for chat in GROUPS:
            row = assignment(chat, new_client_handover={"state": "pending", "revision": 1,
                             "from_id": 11, "to_id": 22, "to_name": "Bea"})
            original = copy.deepcopy(row)
            with self.subTest(chat=chat), patch.object(workflow, "save_assignment") as save, \
                    patch.object(workflow, "queue_message") as queue:
                self.assertEqual(workflow.choose(row, 11, 0, 0, NOW, "pick"), workflow.HANDOVER_DISABLED)
                self.assertEqual(workflow.start_handover(row, 11, 22, "Bea", "", NOW, "start", 0), workflow.HANDOVER_DISABLED)
                self.assertEqual(workflow.finish_handover(row, 22, 1, "yes", NOW, "accept"), workflow.HANDOVER_DISABLED)
                workflow.reconcile_row(row, NOW)
                save.assert_not_called()
                queue.assert_not_called()
                self.assertEqual(row, original)

    def test_veo_directory_keeps_confirmed_owner_and_audit_without_transfer_controls(self):
        for chat in GROUPS:
            row = assignment(chat, new_client_handover={"state": "pending", "to_name": "Bea"},
                             new_client_handover_events=[{"state": "accepted", "to_id": 11}])
            with self.subTest(chat=chat), patch.object(workflow, "history_for", return_value=[row]), \
                    patch.object(clients, "_page_contacts", return_value=[]):
                snapshot = workflow.directory(chat, NOW)
                self.assertEqual(snapshot["clients"][0]["status"], "Assigned")
                self.assertEqual(snapshot["clients"][0]["editor_id"], 11)
                self.assertIsNone(snapshot["clients"][0]["next_editor"])
                self.assertEqual(len(snapshot["clients"][0]["handover_history"]), 1)
                text, buttons = workflow.render_directory(snapshot)
                self.assertNotIn("Handover", text)
                self.assertNotIn("cw:choose:", str(buttons))
                self.assertNotIn("Hand over", str(workflow.owner_buttons(row)))
                self.assertNotIn("handover", workflow.guide(chat).lower())

    def test_queued_transfer_notices_and_pickers_are_suppressed(self):
        for chat in GROUPS:
            with self.subTest(chat=chat), patch.object(workflow, "full_assignment") as read:
                for action in ({"chat_id": chat, "payload": {"client_transfer_assignment_id": 100}},
                               {"chat_id": chat, "dedupe_key": "client-picker:old", "payload": {}}):
                    self.assertFalse(workflow.prepare_delivery(action, NOW))
                read.assert_not_called()

    def test_delivery_refreshes_old_guides_and_removes_queued_handover_buttons(self):
        for chat in GROUPS:
            action = {"chat_id": chat, "dedupe_key": "ready-rollout:v10:old", "payload": {
                "text": "Hand over", "reply_markup": {"inline_keyboard": [[
                    workflow.button("Hand over", "choose:100:0:0"), workflow.button("Client list", "list:0")]]}}}
            self.assertTrue(workflow.prepare_delivery(action, NOW))
            self.assertNotIn("Hand over", str(action["payload"]))
            self.assertIn("Client list", str(action["payload"]))
            generic = {"chat_id": chat, "payload": {"reply_markup": {"inline_keyboard": [[
                workflow.button("Hand over", "choose:100:0:0")], [workflow.button("Client list", "list:0")]]}}}
            self.assertTrue(workflow.prepare_delivery(generic, NOW))
            self.assertEqual(generic["payload"]["reply_markup"]["inline_keyboard"], [[workflow.button("Client list", "list:0")]])

    def test_confirmation_and_instructions_have_no_veo_transfer_prompt(self):
        for chat in GROUPS:
            row = assignment(chat)
            with self.subTest(chat=chat), patch.object(clients, "new_client_actions", return_value=[row]):
                action = {"chat_id": chat, "payload": {"new_client_ack_assignment_id": 100}}
                self.assertTrue(clients.new_client_ack_delivery_allowed(action))
                self.assertNotIn("Hand over", str(action["payload"]))
            with patch.object(ready, "enqueue_scheduled_action", return_value=True) as queue:
                ready.queue_instructions(chat, NOW, GROUPS[chat]["contact_thread"])
                self.assertNotIn("handover", str(queue.call_args_list).lower())

    def test_trabawho_also_disables_handover_controls(self):
        self.assertNotIn("Hand over", str(workflow.owner_buttons(assignment(TRABAWHO_CHAT_ID))))
        self.assertNotIn("/handover", workflow.guide(TRABAWHO_CHAT_ID))

    def test_button_cleanup_edits_only_markup_and_never_sends_replacement(self):
        action = {"chat_id": next(iter(GROUPS)), "action_type": "message", "payload": {
            "edit_reply_markup_message_id": 500, "reply_markup": {"inline_keyboard": []}}}
        with patch.object(sender, "telegram_call", return_value={"message_id": 500}) as api:
            result = sender.send_scheduled_action(action)
        self.assertEqual(result["_telegram_operation"], "editMessageReplyMarkup")
        api.assert_called_once_with("editMessageReplyMarkup", {"chat_id": action["chat_id"],
            "message_id": 500, "reply_markup": {"inline_keyboard": []}})
        for reason in ("message is not modified", "message to edit not found", "message can't be edited"):
            with self.subTest(reason=reason), patch.object(sender, "telegram_call", side_effect=sender.TelegramError(reason)) as api:
                self.assertEqual(sender.send_scheduled_action(action)["message_id"], 500)
                api.assert_called_once()
