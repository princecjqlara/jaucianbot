import datetime as dt
import io
import json
import os
import unittest
from unittest.mock import patch
import veo_automatic_teams as automatic

TENANT = '00000000-0000-4000-8000-000000000001'
CHAT = -100123
ENV = {'VEO_WORKSPACE_ENABLED': 'true', 'VEO_WORKSPACE_TENANT_ID': TENANT,
       'VEO_WORKSPACE_URL': 'https://veo.example', 'VEO_JAUCIAN_SECRET': 'test-only'}


class AutomaticTeamsTests(unittest.TestCase):
    def test_only_verified_numeric_identities_are_checked_and_supervisors_are_first(self):
        accounts = [{'user_id': 'worker', 'role': 'member', 'telegram_user_id': 42},
                    {'user_id': 'unlinked', 'role': 'member', 'telegram_username': 'claimed'},
                    {'user_id': 'leader', 'role': 'team_leader', 'telegram_user_id': 43}]
        def telegram(method, params, **kwargs):
            self.assertEqual(method, 'getChatMember')
            return {'status': 'administrator' if params['user_id'] == 43 else 'member',
                    'user': {'id': params['user_id'], 'is_bot': False}}
        with patch('veo_directory.teams', return_value={CHAT}), patch('veo_directory.workspace_directory', return_value={'accounts': accounts}), \
                patch('veo_directory.group_directory', return_value={'chat_id': CHAT}), patch('telegram_sender.telegram_call', side_effect=telegram) as call:
            result = automatic.snapshot(TENANT, limit=1)
            self.assertEqual([p['user_id'] for p in result['workers']], ['leader'])
            self.assertEqual(result['workers'][0]['membership'], 'administrator')
            self.assertEqual(result['next_offset'], 1)
            result = automatic.snapshot(TENANT, offset=1, limit=1)
            self.assertEqual([p['user_id'] for p in result['workers']], ['worker'])
            self.assertEqual(result['next_offset'], 0)
            self.assertEqual(call.call_count, 2)

    def test_unknown_bot_and_mismatched_responses_never_become_confirmed_members(self):
        account = {'user_id': 'worker', 'role': 'member', 'telegram_user_id': 42}
        cases = [RuntimeError('unavailable'), {'status': 'member', 'user': {'id': 99, 'is_bot': False}},
                 {'status': 'member', 'user': {'id': 42, 'is_bot': True}},
                 {'status': 'unknown', 'user': {'id': 42, 'is_bot': False}}]
        for case in cases:
            with self.subTest(case=case), patch('veo_directory.teams', return_value={CHAT}), \
                    patch('veo_directory.workspace_directory', return_value={'accounts': [account]}), \
                    patch('veo_directory.group_directory', return_value={'chat_id': CHAT}), \
                    patch('telegram_sender.telegram_call', side_effect=case if isinstance(case, Exception) else None,
                          return_value=case if isinstance(case, dict) else None):
                row = automatic.snapshot(TENANT)['workers'][0]
                self.assertFalse(row['membership_confirmed'])
                self.assertEqual(row['membership'], 'unknown')

    def test_restricted_status_retains_telegram_membership_semantics(self):
        for is_member, expected in [(True, 'restricted_member'), (False, 'left')]:
            with patch('veo_directory.teams', return_value={CHAT}), patch('veo_directory.workspace_directory', return_value={
                    'accounts': [{'user_id': 'worker', 'role': 'member', 'telegram_user_id': 42}]}), \
                    patch('veo_directory.group_directory', return_value={'chat_id': CHAT}), \
                    patch('telegram_sender.telegram_call', return_value={'status': 'restricted', 'is_member': is_member, 'user': {'id': 42, 'is_bot': False}}):
                self.assertEqual(automatic.snapshot(TENANT)['workers'][0]['membership'], expected)

    def update(self):
        return {'update_id': 1, 'message': {'text': '/veolink ' + 'a' * 48, 'date': int(self.now.timestamp()),
                'chat': {'id': CHAT}, 'from': {'id': 42, 'is_bot': False}}}

    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)

    def test_link_proves_the_actual_sender_and_uses_the_authenticated_bridge_without_sending_messages(self):
        with patch.dict(os.environ, ENV), patch('veo_directory.teams', return_value={CHAT}), \
                patch('veo_directory.verify_identity') as verify, patch('veo_intake.open_request') as http:
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps({'ok': True, 'tenant_id': TENANT, 'linked': True}).encode())
            self.assertIn('Telegram linked', automatic.handle(self.update(), {CHAT}, self.now))
            verify.assert_called_once_with(CHAT, 42)
            req = http.call_args.args[0]
            body = json.loads(req.data)
            self.assertEqual(body['telegram_user_id'], 42)
            self.assertEqual(body['resource'], 'telegram_link')
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-only')

    def test_forwarded_stale_bot_disabled_and_unapproved_commands_cannot_link(self):
        cases = []
        for field, value in [('forward_origin', {'type': 'user'}), ('date', 0)]:
            update = self.update(); update['message'][field] = value; cases.append(update)
        update = self.update(); update['message']['from']['is_bot'] = True; cases.append(update)
        update = self.update(); update['message']['chat']['id'] = -999; cases.append(update)
        with patch.dict(os.environ, ENV), patch('veo_directory.teams', return_value={CHAT}), \
                patch('veo_directory.verify_identity') as verify, patch('veo_intake.open_request') as http:
            for update in cases:
                self.assertIsNone(automatic.handle(update, {CHAT}, self.now))
            with patch.dict(os.environ, {'VEO_WORKSPACE_ENABLED': 'false'}):
                self.assertIsNone(automatic.handle(self.update(), {CHAT}, self.now))
            verify.assert_not_called(); http.assert_not_called()

    def test_group_link_sends_code_privately_after_verifying_that_group(self):
        update = self.update(); update['message']['text'] = '/link'
        with patch.dict(os.environ, ENV), patch('veo_directory.teams', return_value={CHAT}), \
                patch('veo_directory.verify_identity', return_value={'verified': True}) as verify, \
                patch('telegram_sender.telegram_call', return_value={'message_id': 9}) as send, \
                patch('veo_intake.open_request') as http:
            http.return_value.__enter__.return_value = io.BytesIO(json.dumps({'ok': True, 'tenant_id': TENANT}).encode())
            self.assertIn('sent privately', automatic.handle(update, {CHAT}, self.now))
            verify.assert_called_once_with(CHAT, 42)
            self.assertEqual(send.call_args.args[0], 'sendMessage')
            self.assertEqual(send.call_args.args[1]['chat_id'], 42)
            self.assertIn('code is ', send.call_args.args[1]['text'])
            body = json.loads(http.call_args.args[0].data)
            self.assertEqual(body['resource'], 'telegram_pairing_issue')
