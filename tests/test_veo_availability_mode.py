import datetime as dt
import os
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
import veo_mode
import veo_workspace as workspace
import veo_shift_reports as shifts
import veo_workforce as workforce
from daily_automation import GROUPS

CHAT = next(iter(GROUPS))
ENV = {'VEO_WORKSPACE_ENABLED':'true','VEO_NEW_CLIENTS_ONLY':'true','VEO_AVAILABILITY_ENABLED':'true','VEO_WORKSPACE_TENANT_ID':'tenant'}


class AvailabilityModeTests(unittest.TestCase):
    def test_mode_accepts_only_unmixed_availability_and_keeps_reports_disabled(self):
        with patch.dict(os.environ, ENV):
            action = {'payload':{'veo_availability_date':'2026-11-14','veo_tenant_id':'tenant'}}
            self.assertTrue(veo_mode.availability_action(action))
            for key in ['veo_report_date','veo_submission_id','veo_intake_help']:
                self.assertFalse(veo_mode.availability_action({'payload':{**action['payload'],key:'mixed'}}))
            with patch('veo_shift_reports.queue_availability',return_value=2) as queue, patch('veo_shift_reports.queue') as reports:
                self.assertEqual(workspace.queue_reports(dt.datetime.now(dt.timezone.utc),{CHAT}),2)
                queue.assert_called_once();reports.assert_not_called()

    def test_queue_starts_at_four_pm_and_deduplicates_only_current_mapped_groups(self):
        now=dt.datetime(2026,11,13,16,0,tzinfo=ZoneInfo('Asia/Manila'))
        directory={'teams':[{'telegram_groups_managed':True,'telegram_groups':[{'chat_id':CHAT}]}],
            'accounts':[{'role':'member','telegram_user_id':42,'telegram_groups':[{'chat_id':CHAT}]}]}
        with patch.dict(os.environ,ENV),patch('veo_directory.workspace_directory',return_value=directory) as source,patch('veo_directory.teams',return_value={CHAT}),patch.object(shifts,'request',return_value=[]) as read,patch.object(shifts,'enqueue_scheduled_action',return_value=True) as enqueue:
            self.assertEqual(shifts.queue_availability(now-dt.timedelta(minutes=1),{CHAT}),0);source.assert_not_called()
            self.assertEqual(shifts.queue_availability(now,{CHAT}),1)
            self.assertEqual(enqueue.call_args.kwargs['payload']['veo_availability_date'],'2026-11-14')
            self.assertEqual(enqueue.call_args.kwargs['dedupe_key'],f'veo-active:tenant:{CHAT}:2026-11-14')
            read.return_value=[{'poll_id':'existing'}]
            self.assertEqual(shifts.queue_availability(now,{CHAT}),0);self.assertEqual(enqueue.call_count,1)
            directory['teams'][0]['telegram_groups']=[];read.return_value=[]
            self.assertEqual(shifts.queue_availability(now,{CHAT}),0)

    def test_poll_delivery_uses_normal_checkpoint_and_registration(self):
        action={'id':1,'chat_id':CHAT,'payload':{'veo_availability_date':'2026-11-14','veo_tenant_id':'tenant'}}
        with patch.dict(os.environ,ENV),patch('cloud_store.claim_scheduled_actions',return_value=[action]),patch('cloud_store._update_claim',return_value=True),patch('cloud_store.finish_scheduled_action',return_value=True) as finish,patch.object(shifts,'send_availability',return_value={'message_id':77,'poll':{'id':'poll'}}) as send,patch.object(shifts,'register_availability') as register:
            self.assertEqual(workspace.deliver({CHAT}),(1,1,0))
            send.assert_called_once();register.assert_called_once();self.assertEqual(finish.call_args.kwargs['telegram_message_id'],77)

    def test_votes_are_saved_in_new_client_mode(self):
        tomorrow=str(dt.datetime.now(workforce.MANILA).date()+dt.timedelta(days=1))
        with patch.dict(os.environ,ENV),patch.object(workforce,'request',side_effect=[[{'poll_id':'poll','chat_id':CHAT,'work_date':tomorrow}],[{'id':1}]]) as query:
            workforce.observe_vote({'update_id':10,'poll_answer':{'poll_id':'poll','user':{'id':42,'is_bot':False},'option_ids':[0]}},{CHAT})
        self.assertTrue(query.call_args.args[1]['payload']['active'])
        self.assertEqual(query.call_args.args[1]['payload']['update_id'],10)
