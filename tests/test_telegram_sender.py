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


if __name__ == "__main__":
    unittest.main()
