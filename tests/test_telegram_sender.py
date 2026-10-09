import unittest
from unittest.mock import patch

from telegram_sender import TelegramError, send_scheduled_action


class TelegramSenderTests(unittest.TestCase):
    def test_sends_message_payload(self):
        action = {
            "chat_id": -100123,
            "action_type": "message",
            "payload": {"text": "Announcement", "disable_notification": True},
        }
        with patch("telegram_sender.telegram_call", return_value={"message_id": 9}) as call:
            result = send_scheduled_action(action)
        self.assertEqual(result["message_id"], 9)
        call.assert_called_once_with("sendMessage", {
            "chat_id": -100123,
            "disable_notification": True,
            "text": "Announcement",
        })

    def test_sends_poll_payload(self):
        action = {
            "chat_id": -100123,
            "action_type": "poll",
            "payload": {
                "question": "Lunch?",
                "options": ["Pizza", "Rice"],
                "is_anonymous": False,
                "allows_multiple_answers": True,
            },
        }
        with patch("telegram_sender.telegram_call", return_value={"message_id": 10}) as call:
            send_scheduled_action(action)
        call.assert_called_once_with("sendPoll", {
            "chat_id": -100123,
            "disable_notification": False,
            "question": "Lunch?",
            "options": [{"text": "Pizza"}, {"text": "Rice"}],
            "is_anonymous": False,
            "allows_multiple_answers": True,
        })

    def test_repeat_freebie_uses_reminder_and_reply_prompt(self):
        action = {
            "chat_id": -100123, "action_type": "message", "sent_at": "2026-09-19T01:00:00Z",
            "payload": {"text": "Assignment", "freebie_reminder_text": "Reminder",
                        "parse_mode": "HTML", "reply_markup": {"force_reply": True}},
        }
        with patch("telegram_sender.telegram_call", return_value={"message_id": 11}) as call:
            send_scheduled_action(action)
        self.assertEqual(call.call_args.args[1]["text"], "Reminder")
        self.assertEqual(call.call_args.args[1]["parse_mode"], "HTML")
        self.assertEqual(call.call_args.args[1]["reply_markup"], {"force_reply": True})

    def test_repeat_new_client_uses_working_reminder(self):
        action = {
            "chat_id": -100123, "action_type": "message", "sent_at": "2026-09-19T01:00:00Z",
            "payload": {"text": "New client", "new_client_reminder_text": "Reply WORKING",
                        "parse_mode": "HTML", "reply_markup": {"force_reply": True}},
        }
        with patch("telegram_sender.telegram_call", return_value={"message_id": 12}) as call:
            send_scheduled_action(action)
        self.assertEqual(call.call_args.args[1]["text"], "Reply WORKING")

    def test_edits_existing_instruction_message_without_new_post(self):
        action = {"chat_id": -100123, "action_type": "message",
                  "payload": {"text": "Instructions only", "edit_message_id": 516}}
        with patch("telegram_sender.telegram_call", return_value={"message_id": 516}) as call:
            result = send_scheduled_action(action)
        call.assert_called_once_with("editMessageText", {
            "chat_id": -100123, "message_id": 516, "text": "Instructions only"})
        self.assertEqual(result["_telegram_operation"], "editMessageText")

    def test_deletes_obsolete_poll_without_posting_placeholder(self):
        action = {"chat_id": -100123, "action_type": "message",
                  "payload": {"text": "Placeholder", "delete_message_id": 515}}
        with patch("telegram_sender.telegram_call", return_value={"deleted": True}) as call:
            result = send_scheduled_action(action)
        call.assert_called_once_with("deleteMessage", {"chat_id": -100123, "message_id": 515})
        self.assertEqual(result["message_id"], 515)
        self.assertEqual(result["_telegram_operation"], "deleteMessage")

    def test_message_operation_retries_accept_only_already_done_errors(self):
        for field, error in [
            ("edit_message_id", "Telegram HTTP 400: message is not modified"),
            ("delete_message_id", "Telegram HTTP 400: message to delete not found")]:
            action = {"chat_id": -100123, "action_type": "message",
                      "payload": {"text": "Instructions", field: 515}}
            with patch("telegram_sender.telegram_call", side_effect=TelegramError(error)):
                self.assertEqual(send_scheduled_action(action)["message_id"], 515)
            with patch("telegram_sender.telegram_call", side_effect=TelegramError("Telegram HTTP 403: forbidden")):
                with self.assertRaises(TelegramError):
                    send_scheduled_action(action)

    def test_missing_old_announcement_posts_instruction_only_replacement(self):
        action = {"chat_id": -100123, "action_type": "message",
                  "payload": {"text": "Work instructions", "edit_message_id": 516}}
        with patch("telegram_sender.telegram_call", side_effect=[
                TelegramError("Telegram HTTP 400: Bad Request: message to edit not found"),
                {"message_id": 518}]) as call:
            result = send_scheduled_action(action)
        self.assertEqual(result["message_id"], 518)
        self.assertNotIn("_telegram_operation", result)
        self.assertEqual(call.call_args.args, ("sendMessage", {
            "chat_id": -100123, "text": "Work instructions", "disable_notification": False}))


if __name__ == "__main__":
    unittest.main()
