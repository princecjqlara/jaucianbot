import copy
import datetime as dt
import json
import os
import unittest
from unittest.mock import patch

import capacity_defaults
import live_forecast
import member_presence
import organized_reports as reports
import page_access
import page_capacity
import report_topics
import team_surveys
import tomorrow_capacity
import veo_forecast
import veo_forecast_offers
from daily_automation import MAKATA_CHAT_ID, DAILY_REPORTS_CHAT_ID, GROUPS
from test_wsgi import call_app

NOW=dt.datetime(2026,10,24,9,30,tzinfo=dt.timezone.utc)
DAY=dt.date(2026,10,23)
TARGET=dt.date(2026,10,25)
VEO=next(iter(GROUPS))


def person(uid,values=(0,1,0)):
    first=DAY-dt.timedelta(days=len(values)-1)
    return {'user_id':uid,'name':str(uid),'daily':[{'date':str(first+dt.timedelta(days=i)),
        'attendance':'Active','done_deals':n} for i,n in enumerate(values)]}


class LiveForecastTests(unittest.TestCase):
    def test_cache_signature_changes_for_cohort_history_workdate_and_team(self):
        p=person(7475223633)
        key=live_forecast.cache_key([p],DAY,TARGET,{VEO})
        changed=copy.deepcopy(p);changed['daily'][-1]['done_deals']=9
        self.assertNotEqual(key,live_forecast.cache_key([changed],DAY,TARGET,{VEO}))
        self.assertNotEqual(key,live_forecast.cache_key([p,person(1)],DAY,TARGET,{VEO}))
        self.assertNotEqual(key,live_forecast.cache_key([p],DAY,TARGET+dt.timedelta(days=1),{VEO}))
        self.assertNotEqual(key,live_forecast.cache_key([p],DAY,TARGET,set()))

    def test_cached_forecast_does_not_refit_or_reread_offer_history(self):
        saved={'policy':'cached','work_date':str(TARGET)}
        with patch.object(live_forecast,'request',return_value=[{'payload':{'default':saved}}]), \
             patch.object(live_forecast,'offer_sources',side_effect=AssertionError('no refit')):
            self.assertEqual(live_forecast.baseline([person(1)],DAY,TARGET,{VEO},NOW),saved)

    def test_source_failure_retains_day_model_without_inventing_zero_offers(self):
        expected={'policy':live_forecast.day_engine.POLICY,'person_plans':{},'history_through':str(DAY)}
        with patch.object(live_forecast,'request',side_effect=[[],None]) as write, \
             patch.object(live_forecast,'offer_sources',side_effect=RuntimeError('source down')), \
             patch.object(live_forecast.day_engine,'baseline_for_date',return_value=expected):
            result=live_forecast.baseline([person(7475223633)],DAY,TARGET,{VEO},NOW)
        self.assertTrue(result['live_forecast'])
        self.assertIn('unavailable',result['forecast_source_status'])
        self.assertEqual(write.call_args.args[1]['status'],'cancelled')
        self.assertEqual(result['policy'],live_forecast.day_engine.POLICY)

    def test_dated_offer_source_includes_melo_and_ignores_non_delivery_records(self):
        rows=[{'chat_id':VEO,'id':1,'payload':{}}]
        evidence=[{'kind':'offer','user_id':7475223633,'sent_utc':'2026-10-23T01:00:00+00:00'}]
        with patch.object(live_forecast,'new_client_actions',return_value=rows), \
             patch.object(live_forecast,'build_client_performance',return_value={'evidence':evidence}):
            sources=live_forecast.offer_sources({VEO},DAY,NOW)
        self.assertEqual(sources[0]['activity']['end_date'],'2026-10-24')
        self.assertEqual(sources[0]['activity']['client_performance_evidence'][0]['user_id'],7475223633)

    def test_incomplete_history_is_rejected_before_database_queries(self):
        with patch.object(live_forecast,'request') as read:
            with self.assertRaises(ValueError):live_forecast.baseline([],NOW.date(),TARGET,{VEO},NOW)
            with self.assertRaises(ValueError):live_forecast.baseline([],DAY,DAY,{VEO},NOW)
        read.assert_not_called()

    def test_enrichment_uses_guarded_person_plan_for_melo_and_preserves_other_business(self):
        p=person(7475223633);plan={'policy':veo_forecast_offers.POLICY,'planning_average_per_day':.7}
        default={'policy':veo_forecast_offers.POLICY,'business':'veo','live_forecast':True,'person_plans':{'7475223633':plan}}
        capacities={'members':{str(VEO):[p],'suno':[]},'businesses':{'veo':[p],'suno':['kept']},'defaults':{}}
        with patch.object(live_forecast,'baseline',return_value=default), \
             patch.object(live_forecast,'request',return_value=None):
            result=live_forecast.enrich(capacities,{VEO},DAY,TARGET,NOW)
        self.assertEqual(result['members'][str(VEO)][0]['planning']['planning_average_per_day'],.7)
        self.assertEqual(result['businesses']['suno'],['kept'])
        self.assertEqual(capacity_defaults.apply(p,default)['planning']['planning_average_per_day'],.7)

    def test_membership_refresh_keeps_prepared_model_without_restoring_unverified_workers(self):
        melo=person(7475223633);other=person(2)
        plan={'policy':veo_forecast_offers.POLICY,'planning_average_per_day':.7}
        default={'policy':veo_forecast_offers.POLICY,'live_forecast':True,
            'person_plans':{'7475223633':plan,'2':plan}}
        capacities={'members':{str(VEO):[melo]},'businesses':{'veo':[melo]},'defaults':{}}
        with patch.object(live_forecast,'request',return_value=[{'payload':{'default':default}}]), \
             patch.object(live_forecast,'baseline',side_effect=AssertionError('no refit')):
            result=live_forecast.enrich(capacities,{VEO},DAY,TARGET,NOW,
                scope_people=[melo,other],complete=False)
        self.assertEqual(result['defaults']['veo']['policy'],veo_forecast_offers.POLICY)
        self.assertTrue(result['defaults']['veo']['membership_refresh_pending'])
        self.assertEqual([p['user_id'] for p in result['members'][str(VEO)]],[7475223633])
        self.assertEqual(result['members'][str(VEO)][0]['planning']['planning_average_per_day'],.7)

    def test_cold_membership_sweep_reports_pending_without_fitting_a_partial_peer_model(self):
        capacities={'members':{str(VEO):[]},'businesses':{'veo':[]},
            'defaults':{'veo':{'policy':veo_forecast.POLICY}}}
        with patch.object(live_forecast,'request',return_value=[]), \
             patch.object(live_forecast,'baseline',side_effect=AssertionError('no partial fit')):
            result=live_forecast.enrich(capacities,{VEO},DAY,TARGET,NOW,
                scope_people=[person(7475223633)],complete=False)
        self.assertIn('not prepared yet',result['defaults']['veo']['forecast_source_status'])
        self.assertTrue(result['defaults']['veo']['membership_refresh_pending'])


class StaffingCorrectionTests(unittest.TestCase):
    def test_missing_bot_metadata_is_pending_even_with_an_unexpired_membership_record(self):
        cached={1:{'current':True,'status':'member','expires_at':(NOW+dt.timedelta(minutes=5)).isoformat()}}
        with patch.object(member_presence,'records',return_value=cached):
            people,coverage=member_presence.filter_current(VEO,[{'user_id':1}],NOW,
                humans_only=True,budget={'remaining':0})
        self.assertEqual(people,[])
        self.assertEqual(coverage['pending_verification'],1)
        self.assertEqual(coverage['unverified'],1)

    def test_tomorrow_preview_describes_its_actual_model_and_preserves_pending_membership(self):
        plan={'policy':veo_forecast_offers.POLICY,'planning_average_per_day':.7,
            'safe_plan_per_day':.56,'provisional':False}
        default={'policy':veo_forecast_offers.POLICY,'business':'veo','live_forecast':True,
            'person_plans':{'7475223633':plan}}
        roster=reports.VerifiedRoster([{'user_id':7475223633}],unverified=1)
        data=tomorrow_capacity.build(TARGET,[person(7475223633)],{VEO:roster},
            history_end=DAY,default_override=default)
        self.assertFalse(data['overall']['roster_complete'])
        self.assertIsNone(data['overall']['average_per_all_active'])
        self.assertIn('delivered-offer',data['assumptions'])
        self.assertNotIn('reports are unchanged',data['assumptions'])
        rendered=tomorrow_capacity.render(data)
        self.assertIn('Guarded personal forecast',rendered)
        self.assertIn('Membership checks are pending',rendered)
        self.assertNotIn('uncalibrated trial',rendered)

    def test_old_queued_capacity_post_cannot_reintroduce_a_verified_bot(self):
        action={'chat_id':DAILY_REPORTS_CHAT_ID,'dedupe_key':'organized-daily:capacity',
            'payload':{'organized_report_version':reports.VERSION,'report_source_chat_id':VEO,
                'capacity_member_ids':[8806565601]}}
        with patch.object(report_topics,'enabled',return_value=True), \
             patch.object(member_presence,'records',return_value={8806565601:{'is_bot':True}}), \
             patch.object(member_presence,'confirmed',return_value=True):
            self.assertFalse(report_topics.delivery_allowed(action))

    def test_jaucian_bot_is_removed_from_worker_capacity_and_roster(self):
        bot=8806565601;human=7475223633
        records={uid:{'user_id':uid,'current':True,'status':'member','is_bot':uid==bot,
            'expires_at':(NOW+dt.timedelta(minutes=5)).isoformat()} for uid in (bot,human)}
        capacities={'date':str(DAY),'members':{str(VEO):[person(bot),person(human)]}}
        with patch.object(member_presence,'records',return_value=records):
            result=reports.current_capacities(capacities,{VEO},NOW)
        self.assertEqual([p['user_id'] for p in result['members'][str(VEO)]],[human])
        with patch.object(reports,'daily_poll_counts',return_value={VEO:{'poll_id':'p'}}), \
             patch.object(reports,'daily_poll_active_users',return_value=[{'user_id':bot},{'user_id':human}]), \
             patch.object(page_access,'scoped_voters',side_effect=lambda chat,users,day:users), \
             patch.object(member_presence,'records',return_value=records):
            roster=reports._rosters({VEO},TARGET,NOW)
        self.assertEqual([p['user_id'] for p in roster[VEO]],[human])

    def test_hiding_pepfactory_does_not_shift_poll_part_coverage(self):
        catalog={f'P{i}':str(i) for i in range(8)}
        catalog.update({'PepFactory':'retired','Makata Studio':'makata'})
        answers=[{'user_id':1,'page_ids':[],'page_access_part':0,'answered':True}]
        with patch.object(page_access,'page_map',return_value=catalog), \
             patch.object(page_access,'answer_records',return_value=answers):
            data=page_capacity.load({MAKATA_CHAT_ID})[MAKATA_CHAT_ID]
        self.assertNotIn('PepFactory',data['pages'])
        self.assertNotIn('makata',data['answered'][1])
        self.assertEqual(catalog['PepFactory'],'retired')

    def test_pending_page_access_is_unknown_capacity_not_zero(self):
        rosters={MAKATA_CHAT_ID:[{'user_id':1}]}
        capacities={'members':{str(MAKATA_CHAT_ID):[]},'businesses':{'suno':[]},
            'defaults':{'suno':{'business':'suno','capacity_per_day':.6,'peer_members':3}}}
        inputs={MAKATA_CHAT_ID:{'pages':{'Makata Studio':'m','PepeFactory':'retired'},'access':{},'answered':{}}}
        with patch.dict(page_capacity.SUNO_GROUPS,{MAKATA_CHAT_ID:{'name':'Suno Makata'}}):
            result=page_capacity.build(NOW.date(),{MAKATA_CHAT_ID},rosters,capacities,inputs)
        page=result['businesses'][0]['pages'][0]
        self.assertIsNone(page['total'])
        self.assertEqual(page['missing_access'],1)
        text='\n'.join(page_capacity.render(result,detailed=True))
        self.assertIn('page access pending for 1 Active',text)
        self.assertIn('Estimated total: Unknown',text)
        self.assertNotIn('PepeFactory',text)


class RequestedSurveyTests(unittest.TestCase):
    def setUp(self):
        self.now=NOW;self.target=TARGET;self.chat=MAKATA_CHAT_ID
        self.config={'announcements':4,'contact_thread':13,'active':132,'name':'Suno Makata'}

    def test_request_uses_normal_outbox_and_preserves_existing_poll(self):
        with patch.dict(team_surveys.AVAILABILITY_GROUPS,{self.chat:self.config}), \
             patch.object(team_surveys.automation_controls,'enabled_chat_ids',return_value={self.chat}), \
             patch.object(team_surveys.automation_controls,'page_access_polls_enabled',return_value=True), \
             patch.object(page_access,'enabled',return_value=True), \
             patch.object(team_surveys,'daily_poll_counts',return_value={self.chat:{'poll_id':'kept'}}), \
             patch.object(team_surveys,'queue_daily_polls',return_value=0) as daily, \
             patch.object(page_access,'queue_poll',return_value=0), \
             patch.object(team_surveys,'enqueue_scheduled_action',return_value=True) as queue:
            result=team_surveys.request_refresh([self.chat],TARGET,NOW,{self.chat},'a'*32)
        daily.assert_called_once_with(TARGET,NOW,{self.chat},existing_chats={self.chat})
        self.assertEqual(result['queued_actions'],1)
        self.assertTrue(queue.call_args.kwargs['payload']['staffing_survey_refresh'])
        self.assertEqual(queue.call_args.kwargs['payload']['message_thread_id'],4)

    def test_ambiguous_unapproved_or_non_tomorrow_requests_queue_nothing(self):
        with patch.dict(team_surveys.AVAILABILITY_GROUPS,{self.chat:self.config}), \
             patch.object(team_surveys,'enqueue_scheduled_action') as queue:
            for chats,date in (([],TARGET),([-999],TARGET),([self.chat],DAY)):
                with self.assertRaises(ValueError):team_surveys.request_refresh(chats,date,NOW,{self.chat},'a'*32)
        queue.assert_not_called()

    def test_reminder_links_to_registered_polls_and_distinguishes_ready(self):
        action={'chat_id':self.chat,'payload':{'staffing_survey_refresh':True,'staffing_survey_date':str(TARGET)}}
        with patch.dict(team_surveys.AVAILABILITY_GROUPS,{self.chat:self.config}), \
             patch.object(team_surveys.availability_reminders,'poll',return_value={'telegram_message_id':100}), \
             patch.object(page_access,'page_map',return_value={'Makata Studio':'a'}), \
             patch.object(page_access,'poll_record',return_value={'message_id':101}):
            self.assertTrue(team_surveys.prepare_delivery(action,NOW))
        text=action['payload']['text']
        self.assertIn('/100',text);self.assertIn('/101',text)
        self.assertIn('2026-10-25',text);self.assertIn('Ready',text)
        self.assertEqual(action['payload']['parse_mode'],'HTML')

    def test_unregistered_polls_wait_and_old_requests_retire(self):
        action={'chat_id':self.chat,'payload':{'staffing_survey_refresh':True,'staffing_survey_date':str(TARGET)}}
        with patch.dict(team_surveys.AVAILABILITY_GROUPS,{self.chat:self.config}), \
             patch.object(team_surveys.availability_reminders,'poll',return_value=None), \
             patch.object(page_access,'page_map',return_value={'Makata Studio':'a'}), \
             patch.object(page_access,'poll_record',return_value=None):
            self.assertFalse(team_surveys.prepare_delivery(action,NOW))
            self.assertFalse(action['payload'].get('_staffing_survey_obsolete',False))
            self.assertFalse(team_surveys.prepare_delivery(action,NOW+dt.timedelta(days=1)))
        self.assertTrue(action['payload']['_staffing_survey_obsolete'])

    def test_survey_endpoint_requires_auth_and_explicit_groups(self):
        payload=json.dumps({'groups':[self.chat],'work_date':str(TARGET),'request_id':'a'*32}).encode()
        with patch.dict(os.environ,{'INSIGHTS_API_KEY':'key','ALLOWED_CHAT_IDS':str(self.chat)}), \
             patch('app.makata_automation.hydrate'),patch.object(team_surveys,'request_refresh',return_value={'ok':True}) as request:
            self.assertEqual(call_app('/api/reports/surveys',method='POST',body=payload)[0],401)
            status,_=call_app('/api/reports/surveys',method='POST',body=payload,headers={'Authorization':'Bearer key'})
            self.assertEqual(status,200)
            self.assertEqual(request.call_args.args[0],[self.chat])
            self.assertEqual(call_app('/api/reports/surveys',method='GET')[0],405)


if __name__=='__main__':unittest.main()
