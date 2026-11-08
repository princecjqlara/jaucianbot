import datetime as dt
import io
import json
import os
import unittest
import uuid
from unittest.mock import patch
import veo_intake as intake
from daily_automation import GROUPS

CHAT = next(iter(GROUPS))
TENANT = '00000000-0000-4000-8000-000000000001'
REFERENCE = '00000000-0000-4000-8000-000000000002'
ENV = {'VEO_WORKSPACE_ENABLED': 'true', 'VEO_TELEGRAM_INTAKE_ENABLED': 'true', 'VEO_WORKSPACE_TENANT_ID': TENANT,
       'VEO_WORKSPACE_URL': 'https://veochatbot.vercel.app', 'VEO_JAUCIAN_SECRET': 'private-test-only'}


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        for mocked in (patch.dict(os.environ, ENV), patch('automation_controls.new_clients_enabled', return_value=True),
                       patch('veo_directory.verify_identity', return_value={'verified': True})):
            mocked.start(); self.addCleanup(mocked.stop)

    def update(self, text='/ready', callback=None):
        message = {'chat': {'id': CHAT}, 'message_id': 99, 'date': int(self.now.timestamp()), 'text': text, 'from': {'id': 42, 'is_bot': False}}
        if callback:
            return {'update_id': 100, 'callback_query': {'id': 'callback-id', 'from': message['from'], 'message': message, 'data': callback}}
        return {'update_id': 100, 'message': message}

    def result(self):
        return {'ok': True, 'tenant_id': TENANT, 'request_id': REFERENCE, 'chat_id': CHAT, 'telegram_user_id': 42,
                'text': 'Current Tokko controls', 'reply_markup': {'inline_keyboard': [[{'text': 'Ready', 'callback_data': 'vi:' + REFERENCE}]]}}

    def test_ready_and_callbacks_use_verified_numeric_identity_and_stable_update_references(self):
        with patch.object(intake, 'call', return_value=self.result()) as source, patch.object(intake, 'enqueue_scheduled_action', return_value=True) as enqueue:
            for _ in range(2): self.assertIn('Saved in Tokko', intake.handle(self.update(), {CHAT}, self.now))
            first, second = source.call_args_list
            self.assertEqual(first, second); self.assertEqual(first.args, ('ready',)); self.assertEqual(first.kwargs['user'], 42)
            uuid.UUID(first.kwargs['data']['request_id'])
            self.assertEqual(enqueue.call_args_list[0].kwargs['dedupe_key'], enqueue.call_args_list[1].kwargs['dedupe_key'])
            intake.handle(self.update(callback='vi:' + REFERENCE), {CHAT}, self.now)
            self.assertEqual(source.call_args.args, ('callback',)); self.assertEqual(source.call_args.kwargs['data']['token_id'], REFERENCE)

    def test_disabled_unapproved_bot_forwarded_and_stale_messages_cannot_change_tokko(self):
        cases = [self.update(), self.update(), self.update(), self.update()]
        cases[0]['message']['chat']['id'] = -999
        cases[1]['message']['from']['is_bot'] = True
        cases[2]['message']['date'] -= 601
        cases[3]['message']['forward_origin'] = {'type': 'hidden_user'}
        with patch.object(intake, 'call') as source:
            for update in cases: self.assertIsNone(intake.handle(update, {CHAT}, self.now))
            with patch.dict(os.environ, {'VEO_TELEGRAM_INTAKE_ENABLED': 'false'}): self.assertIsNone(intake.handle(self.update(), {CHAT}, self.now))
            source.assert_not_called()

    def test_old_assignment_buttons_cannot_write_a_parallel_owner(self):
        with patch.object(intake, 'call') as source, patch.object(intake, 'enqueue_scheduled_action') as enqueue:
            self.assertIn('Use /clients', intake.handle(self.update(callback='cw:take:old-offer'), {CHAT}, self.now))
            source.assert_not_called(); enqueue.assert_not_called()

    def test_unmapped_accounts_get_generic_setup_help_and_network_failures_remain_retryable(self):
        with patch.object(intake, 'call', side_effect=intake.IntakeScopeChanged('Verify your identity')), patch.object(intake, 'enqueue_scheduled_action', return_value=True) as enqueue:
            intake.handle(self.update(), {CHAT}, self.now)
            self.assertTrue(enqueue.call_args.kwargs['payload']['veo_intake_help'])
            self.assertIn('team leader', enqueue.call_args.kwargs['payload']['text'])
            self.assertIn('A manager is optional', enqueue.call_args.kwargs['payload']['text'])
            self.assertNotIn('veo_intake_request_id', enqueue.call_args.kwargs['payload'])
            enqueue.reset_mock(); intake.handle(self.update(callback='vi:' + REFERENCE), {CHAT}, self.now); enqueue.assert_not_called()
        with patch.object(intake, 'call', side_effect=RuntimeError('network')), patch.object(intake, 'enqueue_scheduled_action') as enqueue:
            with self.assertRaises(RuntimeError): intake.handle(self.update(), {CHAT}, self.now)
            enqueue.assert_not_called()

    def test_offer_notifications_are_deduplicated_by_offer_and_destination(self):
        offer = {'offer_id': REFERENCE, 'telegram_user_id': 42, 'chat_id': CHAT}
        key = f'veo-intake-offer:{TENANT}:{REFERENCE}:{CHAT}'
        with patch.object(intake, 'call', return_value={'offers': [offer]}), patch.object(intake, 'request', return_value=[]), patch.object(intake, 'enqueue_scheduled_action', return_value=True) as enqueue:
            self.assertEqual(intake.queue(self.now, {CHAT}), 1); self.assertEqual(enqueue.call_args.kwargs['dedupe_key'], key)
            self.assertEqual(enqueue.call_args.kwargs['payload']['veo_intake_offer_id'], REFERENCE)
            self.assertFalse({'contact_name', 'photo_url', 'client_details'} & enqueue.call_args.kwargs['payload'].keys())
        with patch.object(intake, 'call', return_value={'offers': [offer]}), patch.object(intake, 'request', return_value=[{'dedupe_key': key}]), patch.object(intake, 'enqueue_scheduled_action') as enqueue:
            self.assertEqual(intake.queue(self.now, {CHAT}), 0); enqueue.assert_not_called()

    def action(self):
        return {'chat_id': CHAT, 'payload': {'veo_intake_request_id': REFERENCE, 'veo_intake_user_id': 42, 'veo_tenant_id': TENANT}}

    def test_delivery_renders_current_access_before_sending_and_rejects_scope_or_identity_changes(self):
        with patch('veo_directory.teams', return_value={CHAT}), patch.object(intake, 'call', return_value=self.result()) as source, patch('telegram_sender.telegram_call', return_value={'message_id': 7}) as send:
            self.assertEqual(intake.send(self.action())['message_id'], 7); self.assertEqual(source.call_args.args, ('render',))
            self.assertEqual(send.call_args.args[0], 'sendMessage'); self.assertEqual(send.call_args.args[1]['reply_markup'], self.result()['reply_markup'])
        with patch('veo_directory.teams', return_value={CHAT}), patch.object(intake, 'call', side_effect=intake.IntakeScopeChanged('Page revoked')), patch('telegram_sender.telegram_call') as send:
            with self.assertRaises(intake.IntakeScopeChanged): intake.send(self.action())
            send.assert_not_called()
        with patch('veo_directory.teams', return_value={CHAT}), patch.object(intake, 'call', return_value={**self.result(), 'telegram_user_id': 99}), patch('telegram_sender.telegram_call') as send:
            with self.assertRaises(intake.IntakeScopeChanged): intake.send(self.action())
            send.assert_not_called()

    def test_foreign_links_unbound_buttons_and_expired_help_are_not_sent(self):
        bad = {**self.result(), 'reply_markup': {'inline_keyboard': [[{'text': 'Wrong', 'callback_data': 'cw:take:anyone'}]]}}
        with patch('veo_directory.teams', return_value={CHAT}), patch.object(intake, 'call', return_value=bad), patch('telegram_sender.telegram_call') as send:
            with self.assertRaises(ValueError): intake.send(self.action())
            send.assert_not_called()
        help_action = {'chat_id': CHAT, 'payload': {'veo_intake_help': True, 'veo_tenant_id': TENANT, 'veo_intake_user_id': 42, 'expires_at': (self.now-dt.timedelta(seconds=1)).isoformat(), 'text': 'setup'}}
        with patch('veo_directory.teams', return_value={CHAT}), patch('telegram_sender.telegram_call') as send:
            with self.assertRaises(intake.IntakeScopeChanged): intake.send(help_action)
            send.assert_not_called()

    def test_acknowledged_assignment_messages_reuse_the_existing_outbox_checkpoint(self):
        import veo_workspace
        action = {**self.action(), 'id': 1}; action['payload']['_veo_message_id'] = 77
        with patch('cloud_store.claim_scheduled_actions', return_value=[action]), patch('cloud_store.finish_scheduled_action', return_value=True) as finish, patch.object(intake, 'send') as send:
            self.assertEqual(veo_workspace.deliver({CHAT}), (1, 1, 0)); send.assert_not_called(); self.assertEqual(finish.call_args.kwargs['telegram_message_id'], 77)

    def test_bridge_is_authenticated_tenant_bound_and_does_not_follow_redirects(self):
        response = io.BytesIO(json.dumps(self.result()).encode())
        with patch.object(intake, 'open_request', return_value=response) as opened:
            self.assertTrue(intake.call('pool', user=42, chat=CHAT)['ok'])
            req = opened.call_args.args[0]; self.assertEqual(req.full_url, 'https://veochatbot.vercel.app/api/workspace/handoff')
            self.assertEqual(req.get_header('Authorization'), 'Bearer private-test-only')
        with patch.object(intake, 'open_request', return_value=io.BytesIO(json.dumps({**self.result(), 'tenant_id': str(uuid.uuid4())}).encode())):
            with self.assertRaises(RuntimeError): intake.call('pool', user=42, chat=CHAT)
        with self.assertRaises(RuntimeError): intake.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://foreign.test')


if __name__ == '__main__':
    unittest.main()
