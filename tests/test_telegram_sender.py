import unittest
from unittest.mock import patch

from telegram_sender import send_scheduled_action


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


if __name__ == "__main__":
    unittest.main()
