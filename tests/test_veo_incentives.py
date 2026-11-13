import datetime as dt
import unittest
from unittest.mock import patch
import veo_incentives
import veo_mode

NOTICE = '00000000-0000-4000-8000-000000000001'
TENANT = '00000000-0000-4000-8000-000000000002'


class IncentivesTests(unittest.TestCase):
    def test_receive_queues_one_deduplicated_notice_without_sending(self):
        row = {'id': NOTICE, 'tenant_id': TENANT, 'telegram_chat_id': -123}
        with patch.object(veo_incentives, 'authoritative', return_value=row), patch.object(veo_incentives, 'enqueue_scheduled_action') as enqueue:
            result = veo_incentives.receive({'notice_id': NOTICE}, None, lambda _, status, body: (status, body))
        self.assertEqual(result, (200, {'ok': True}))
        self.assertEqual(enqueue.call_args.kwargs['dedupe_key'], 'veo-incentive:' + NOTICE)
        self.assertNotIn('text', enqueue.call_args.kwargs['payload'])

    def test_send_rechecks_authoritative_destination_and_text(self):
        row = {'telegram_chat_id': -123, 'telegram_thread_id': 99, 'text': 'Incentive +5%'}
        action = {'chat_id': -123, 'payload': {'veo_incentive_notice': NOTICE}}
        with patch.object(veo_incentives, 'authoritative', return_value=row), patch('telegram_sender.telegram_call', return_value={'message_id': 7}) as send:
            self.assertEqual(veo_incentives.send(action)['message_id'], 7)
            self.assertEqual(send.call_args.args[1]['message_thread_id'], 99)
            self.assertEqual(send.call_args.args[1]['text'], row['text'])
            action['chat_id'] = -999
            with self.assertRaisesRegex(ValueError, 'destination changed'):
                veo_incentives.send(action)
            self.assertEqual(send.call_count, 1)

    def test_incentives_require_enabled_approved_deal_workflow(self):
        action = {'payload': {'veo_incentive_notice': NOTICE, 'veo_tenant_id': TENANT}}
        with patch.dict('os.environ', {'VEO_APPROVED_DEALS_ENABLED': 'true'}):
            self.assertTrue(veo_mode.reviewed_action(action))
        with patch.dict('os.environ', {'VEO_APPROVED_DEALS_ENABLED': 'false'}):
            self.assertFalse(veo_mode.reviewed_action(action))
