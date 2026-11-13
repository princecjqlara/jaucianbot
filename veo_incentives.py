"""Durable incentive announcements; CRM is authoritative, scheduler sends."""
import datetime as dt
import json
import os
import urllib.parse
import urllib.request
from cloud_store import request
import uuid
from cloud_store import allowed_chat_ids, enqueue_scheduled_action
from automation_controls import chat_enabled


def authoritative(notice_id):
    uuid.UUID(notice_id)
    base = os.environ.get('VEO_WORKSPACE_URL', '').rstrip('/')
    secret = os.environ.get('VEO_JAUCIAN_SECRET', '')
    if not base.startswith('https://') or not secret:
        raise ValueError('Secure workspace connection required')
    req = urllib.request.Request(base + '/api/workspace/handoff?' + urllib.parse.urlencode({'incentive_notice': notice_id}), headers={'Authorization': 'Bearer ' + secret})
    with urllib.request.urlopen(req, timeout=15) as response:
        data = json.load(response)
    from daily_automation import GROUPS
    chat = int(data['telegram_chat_id'])
    if data.get('id') != notice_id or data.get('tenant_id') != os.environ.get('VEO_WORKSPACE_TENANT_ID') or chat not in GROUPS or chat not in allowed_chat_ids() or not chat_enabled(chat):
        raise ValueError('Incentive destination is not approved')
    if not isinstance(data.get('text'), str) or not 1 <= len(data['text']) <= 4000:
        raise ValueError('Invalid incentive announcement')
    return data


def receive(data, start_response, respond):
    current = authoritative(data['notice_id'])
    payload = {'veo_incentive_notice': current['id'], 'veo_tenant_id': current['tenant_id']}
    key = 'veo-incentive:' + current['id']
    queued = enqueue_scheduled_action(chat_id=int(current['telegram_chat_id']), action_type='message', scheduled_for=dt.datetime.now(dt.timezone.utc),
        dedupe_key=key, payload=payload)
    if not queued and not request('scheduled_actions?' + urllib.parse.urlencode({'dedupe_key': 'eq.' + key, 'select': 'id', 'limit': 1})):
        raise RuntimeError('Incentive notification queue unavailable')
    return respond(start_response, 200, {'ok': True})


def send(action):
    data = authoritative(action['payload']['veo_incentive_notice'])
    if int(data['telegram_chat_id']) != int(action['chat_id']):
        raise ValueError('Incentive destination changed; review team routing')
    from telegram_sender import telegram_call
    payload = {'chat_id': int(action['chat_id']), 'text': data['text']}
    if data.get('telegram_thread_id'):
        payload['message_thread_id'] = int(data['telegram_thread_id'])
    return telegram_call('sendMessage', payload)
