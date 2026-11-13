import unittest
from unittest.mock import patch
import veo_receipt_deletion as deletion
import veo_workspace as workspace

SUBMISSION = '00000000-0000-4000-8000-000000000001'


class ReceiptDeletionTests(unittest.TestCase):
    def action(self):
        return {'id': 9, 'chat_id': -123, 'payload': {'veo_receipt_delete': True,
            'veo_submission_id': SUBMISSION, 'veo_delete_source_action': 4}}

    def source(self, **extra):
        return {'id': 4, 'chat_id': -123, 'status': 'sent', 'telegram_message_id': 10,
            'payload': {'veo_submission_id': SUBMISSION, '_veo_message_id': 10,
                '_veo_album_message_ids': [10, 11, 12], '_veo_details_message_id': 13}, **extra}

    def test_refuses_a_receipt_without_an_authoritative_tombstone(self):
        with patch.object(workspace, 'authoritative', return_value={}), patch.object(deletion, 'request') as query:
            with self.assertRaisesRegex(ValueError, 'has not been deleted'):
                deletion.deliver(self.action())
            query.assert_not_called()

    def test_removes_every_album_and_details_message_and_checkpoints_progress(self):
        action = self.action()
        with patch.object(workspace, 'authoritative', side_effect=workspace.ReceiptDeleted()), patch.object(deletion, 'request', return_value=[self.source()]), patch('telegram_sender.telegram_call') as telegram, patch('cloud_store._update_claim', return_value=True) as checkpoint:
            self.assertTrue(deletion.deliver(action))
        self.assertEqual([c.args[1]['message_id'] for c in telegram.call_args_list], [10, 11, 12, 13])
        self.assertEqual(checkpoint.call_count, 4)
        self.assertEqual(action['payload']['veo_deleted_message_ids'], [10, 11, 12, 13])

    def test_retries_skip_completed_messages_and_missing_messages_are_idempotent(self):
        from telegram_sender import TelegramError
        action = self.action();action['payload']['veo_deleted_message_ids'] = [10, 11, 12]
        with patch.object(workspace, 'authoritative', side_effect=workspace.ReceiptDeleted()), patch.object(deletion, 'request', return_value=[self.source()]), patch('telegram_sender.telegram_call', side_effect=TelegramError('Bad Request: message to delete not found')) as telegram, patch('cloud_store._update_claim', return_value=True):
            self.assertTrue(deletion.deliver(action))
        telegram.assert_called_once_with('deleteMessage', {'chat_id': -123, 'message_id': 13})

    def test_waits_for_inflight_delivery_and_rejects_cross_chat_attribution(self):
        for source in [self.source(status='processing'), self.source(chat_id=-456)]:
            with patch.object(workspace, 'authoritative', side_effect=workspace.ReceiptDeleted()), patch.object(deletion, 'request', return_value=[source]), patch('telegram_sender.telegram_call') as telegram:
                with self.assertRaises((ValueError, RuntimeError)):
                    deletion.deliver(self.action())
                telegram.assert_not_called()

    def test_schedules_removal_without_sending_in_the_request(self):
        source = self.source()
        with patch.object(workspace, 'authoritative', side_effect=workspace.ReceiptDeleted()), patch.object(deletion, 'request', side_effect=[[source], [], [{'id': 9}], []]), patch.object(deletion, 'enqueue_scheduled_action', return_value=True) as enqueue, patch('telegram_sender.telegram_call') as telegram:
            deletion.receive({'submission_id': SUBMISSION}, None, lambda *args: args[-1])
        self.assertEqual(enqueue.call_args.kwargs['payload']['veo_delete_source_action'], 4)
        telegram.assert_not_called()
