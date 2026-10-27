"""Transition, evidence, migration and read-only UI acceptance checks."""
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from sqlalchemy import select, text
    from sqlalchemy.engine import make_url
    from jp_screening.analysis.db import Database
except ModuleNotFoundError:
    raise unittest.SkipTest('Install .[analysis].')

from jp_screening.core import JST, ScreeningError
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.domain import digest
from jp_screening.analysis.ingest import import_run
from jp_screening.analysis.monitoring import build_periods, representatives, interval, rebuild, bucket, summary
from jp_screening.analysis.research import evidence_for, validate_extraction, automatic_research
from jp_screening.analysis.sources import collect, save_source, matches_company, public_url
from jp_screening.analysis.tracking import save_prices
from analysis_support import START, DAYS, NOW, CALENDAR, run_folder, result, quotes, FakeLLM, FakeAPI


def run(day, name='r1', hour=18, stage=2):
    return dict(id=name, asof=day, created_at=datetime.combine(day,time(hour),JST), eligible=True, stage=stage, rules_key='rules')


def row(run_id='r1', code='10000', flags=('通過','通過')):
    return dict(run_id=run_id, code=code, name='合成企業', s1=flags[0], s2=flags[1],
        close=D(100), features={'long_low_rise':'10'}, payload={'stage1_reason':'許容上限超過','stage2_reason':'5週平均以下'})


class MonitoringTests(unittest.TestCase):
    def test_continuous_pass_exit_and_reentry_are_distinct(self):
        runs=[run(DAYS[i],f'r{i}') for i in range(4)]
        rows=[row(f'r{i}',flags=('通過','不通過') if i==2 else ('通過','通過')) for i in range(4)]
        periods, observations=build_periods(runs,rows,set(DAYS))
        self.assertEqual(len(periods),2)
        self.assertEqual(periods[0]['end_date'],DAYS[2])
        self.assertIsNone(periods[1]['end_date'])
        self.assertEqual(len(observations),4)

    def test_unknown_missing_and_exclusion_do_not_end_period(self):
        runs=[run(DAYS[i],f'r{i}') for i in range(5)]
        rows=[row('r0'),row('r1',flags=('判定不能','不通過')),row('r3',flags=('対象外','未対象')),row('r4')]
        periods,_=build_periods(runs,rows,set(DAYS))
        self.assertEqual(len(periods),1)
        self.assertTrue(periods[0]['continuity_unknown'])
        self.assertIsNone(periods[0]['end_date'])

    def test_gap_is_not_a_fabricated_failure_and_monthly_failure_ends(self):
        periods,obs=build_periods([run(DAYS[0]),run(DAYS[3],'r2')],
            [row(),row('r2',flags=('不通過','未対象'))],set(DAYS))
        self.assertEqual(len(obs),2)
        self.assertTrue(periods[0]['continuity_unknown'])
        self.assertEqual(periods[0]['failure']['stage'],1)
        self.assertEqual(periods[0]['end_date'],DAYS[3])

    def test_latest_full_run_beats_older_deeper_run(self):
        old=run(START,'older',18,3)
        new=run(START,'newer',19,2)
        ignored={**new,'id':'demo','eligible':False}
        self.assertEqual([r['id'] for r in representatives([old,new,ignored])],['newer'])
        periods,_=build_periods([old,new],[row('older'),row('newer',flags=('通過','不通過'))],set(DAYS))
        self.assertFalse(periods)

    def test_split_adjustment_does_not_create_loss(self):
        prices={DAYS[0]:{'C':100,'H':100,'L':100,'AdjFactor':D('.2')},
                DAYS[1]:{'C':50,'H':55,'L':45,'AdjFactor':D('.5')}}
        value=interval(prices,set(DAYS),DAYS[0],DAYS[1])
        self.assertEqual(value['return_pct'],0)
        self.assertEqual(value['max_up_pct'],10)
        self.assertEqual(value['max_down_pct'],-10)
        prices[DAYS[1]]['C']=60
        self.assertEqual(interval(prices,set(DAYS),DAYS[0],DAYS[1])['return_pct'],20)

    def test_missing_endpoint_never_substitutes_previous_day(self):
        prices={DAYS[0]:{'C':100,'AdjFactor':1},DAYS[1]:{'C':110,'AdjFactor':1}}
        self.assertIsNone(interval(prices,set(DAYS),DAYS[0],DAYS[2])['return_pct'])
        prices[DAYS[2]]={'C':None,'AdjFactor':1}
        self.assertEqual(interval(prices,set(DAYS),DAYS[0],DAYS[2])['status'],'必要価格の欠損')

    def test_bad_adjustment_is_not_zero_return(self):
        prices={DAYS[0]:{'C':100,'AdjFactor':1},DAYS[1]:{'C':100,'AdjFactor':0}}
        value=interval(prices,set(DAYS),DAYS[0],DAYS[1])
        self.assertEqual(value['status'],'調整係数の欠損')
        self.assertIsNone(value['return_pct'])

    def test_citations_must_be_real_exact_body_spans(self):
        body='合成企業は新しい製品について事業提携を発表しました。'
        source={'id':'e1','body':body,'title':'提携','url':'https://example.org/item','published_at':NOW}
        good={'factors':[{'source_id':'e1','quote':body,'category':'事業・提携'}]}
        self.assertEqual(validate_extraction(good,[source])[0]['source_id'],'e1')
        for field,value in [('source_id','invented'),('quote','利益が999999倍になりました。')]:
            invalid=json.loads(json.dumps(good))
            invalid['factors'][0][field]=value
            with self.assertRaises(ScreeningError): validate_extraction(invalid,[source])

    def test_after_close_unknown_date_headline_and_unmatched_excluded(self):
        period={'code':'10000','start_date':START,'end_date':DAYS[1]}
        source=dict(id='1',code='10000',provider='test',source_key='1',title='t',body='十分な根拠本文',
            content={'matched':True,'body_state':'本文取得済み'},fetched_at=NOW,
            published_at=datetime.combine(DAYS[1],time(14),JST))
        cases=[source,{**source,'id':'2','source_key':'2','published_at':datetime.combine(DAYS[1],time(16),JST)},
               {**source,'id':'3','source_key':'3','published_at':None},
               {**source,'id':'4','source_key':'4','body':''},
               {**source,'id':'5','source_key':'5','content':{'matched':False}}]
        self.assertEqual([r['id'] for r in evidence_for(period,cases)],['1'])
        withdrawn={**source,'id':'6','fetched_at':NOW+timedelta(seconds=1),'body':'','content':{'body_state':'取下げ・不開示'}}
        self.assertFalse(evidence_for(period,[source,withdrawn]))

    def test_company_matching_rejects_ambiguous_short_names(self):
        self.assertTrue(matches_company('合成製薬株式会社が決算発表','10000',['合成製薬株式会社']))
        self.assertFalse(matches_company('Taya visited the city','10000',['TAYA']))
        self.assertFalse(matches_company('ほかの会社のニュース','10000',['合成製薬株式会社']))

    def test_local_article_urls_rejected_before_network_request(self):
        for url in ['http://example.org','https://127.0.0.1/item','https://user:secret@example.org/item']:
            with self.assertRaises(ScreeningError): public_url(url)


    def test_global_conclusion_requires_unique_securities_and_separates_rules(self):
        base={'active':True,'state':'ended','rules_key':'r','end_date':DAYS[1]}
        periods=[]
        for direction in ('up','down'):
            for i in range(10):
                periods.append({**base,'id':direction+str(i),'code':str(i), 'direction':direction,
                    'content':{'continuity_unknown':False,'end_date':str(DAYS[1]),
                        'start_snapshot':{'features':{'long_low_rise':'10'}},
                        'failure':{'reason':'条件不成立'}, 'exit':{'return_pct':'10' if direction=='up' else '-10','trading_days':5}}})
        report=summary(periods)
        self.assertEqual(report['rules']['r']['state'],'比較可能')
        fewer=[p for p in periods if p['id']!='down9']
        self.assertEqual(summary(fewer)['rules']['r']['state'],'蓄積不足')
        repeated=[{**p,'code':'same'} for p in periods]
        self.assertEqual(summary(repeated)['rules']['r']['state'],'蓄積不足')
        split=[{**p,'rules_key':p['direction']} for p in periods]
        self.assertTrue(all(v['state']=='蓄積不足' for v in summary(split)['rules'].values()))

    def test_news_article_uses_publisher_date_not_detection_time(self):
        from jp_screening.analysis.sources import PublicHTTP
        html=('<html><script type="application/ld+json">{"@graph":[{"datePublished":"2026-01-05T12:00:00+09:00"}]}</script>'
              '<article>'+('合成企業は決算を発表しました。'*20)+'</article></html>').encode()
        class HTTP(PublicHTTP):
            def get(self,url,**kwargs):
                if url.endswith('/robots.txt'): return b'User-agent: *\nAllow: /','text/plain',url
                return html,'text/html',url
        with patch('jp_screening.analysis.sources.public_url',side_effect=lambda url:url):
            body,published,state=HTTP().article('https://example.org/news')
        self.assertEqual(state,'本文取得済み')
        self.assertEqual(published,datetime(2026,1,5,12,tzinfo=JST))
        self.assertIn('合成企業',body)


TEST_URL=os.environ.get('ANALYSIS_TEST_DATABASE_URL')


@unittest.skipUnless(TEST_URL,'Disposable PostgreSQL required')
class MonitoringPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not make_url(TEST_URL).database.endswith('_test'):
            raise RuntimeError('Disposable database required')
        cls.db=Database(TEST_URL)
        cls.db.upgrade()

    def setUp(self):
        with self.db.transaction() as conn:
            for table in reversed(s.metadata.sorted_tables): conn.execute(table.delete())
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=Config(self.root,TEST_URL,TEST_URL,'http://127.0.0.1:11434','fake:1','fake:1','fake',False,
                           research_enabled=False)

    def rows(self,table):
        with self.db.transaction(readonly=True) as conn:
            return [dict(r) for r in conn.execute(select(table)).mappings()]

    def load_ended(self,ending=110):
        import_run(self.db,run_folder(self.root,day=START))
        fail=result(flags=('不通過','未対象','未対象'),day=DAYS[1])
        fail.update(stage1_reason='許容上限超過',reason='許容上限超過',condition_a=False)
        import_run(self.db,run_folder(self.root,name='exit',day=DAYS[1],results=[fail]))
        raw=quotes(days=DAYS[:2]); raw[-1]['C']=ending
        save_prices(self.db,'10000',raw,NOW)
        rebuild(self.db,datetime.combine(DAYS[1],time(19),JST))
        return raw

    def test_csv_stage2_membership_and_rebuild_are_idempotent(self):
        source=[result(),result('20000',('通過','通過','不通過')),result('30000',('通過','不通過','未対象'))]
        import_run(self.db,run_folder(self.root,results=source))
        rebuild(self.db,NOW); rebuild(self.db,NOW)
        self.assertEqual({r['code'] for r in self.rows(s.monitor_periods)},{'10000','20000'})
        self.assertEqual(len(self.rows(s.monitor_days)),2)
        self.assertFalse(self.rows(s.monitor_revisions))

    def test_exit_class_stays_fixed_after_future_prices_and_revision_retained(self):
        raw=self.load_ended()
        period=self.rows(s.monitor_periods)[0]
        self.assertEqual(bucket(period),'上昇して通過終了')
        extended=raw+[dict(Date=str(DAYS[2]),Code='10000',O=70,H=80,L=60,C=70,AdjFactor=1)]
        save_prices(self.db,'10000',extended,NOW)
        rebuild(self.db,datetime.combine(DAYS[2],time(19),JST))
        self.assertEqual(self.rows(s.monitor_periods)[0]['direction'],'up')
        self.assertEqual(D(self.rows(s.monitor_periods)[0]['content']['latest']['return_pct']),-30)
        self.assertTrue(self.rows(s.monitor_revisions))
        before=self.rows(s.results)
        raw[-1]['C']=90
        save_prices(self.db,'10000',raw,NOW)
        rebuild(self.db,datetime.combine(DAYS[2],time(19),JST))
        self.assertEqual(self.rows(s.monitor_periods)[0]['direction'],'down')
        self.assertEqual(self.rows(s.results),before)

    def test_no_evidence_still_saves_honest_conclusion_without_llm(self):
        self.load_ended()
        llm=FakeLLM()
        with patch.object(llm,'chat',side_effect=AssertionError('No evidence means no generation')):
            first=automatic_research(self.db,self.config,NOW,ollama=llm)
            second=automatic_research(self.db,self.config,NOW,ollama=llm)
        self.assertEqual(first,second)
        individual=[r for r in self.rows(s.monitor_analyses) if r['kind']=='individual'][0]
        self.assertIn('要因を特定できない',individual['content']['conclusion'])
        self.assertEqual(len(self.rows(s.monitor_analyses)),2)

    def test_llm_failure_resumes_and_retrieval_validates_citation(self):
        self.load_ended()
        body='合成企業は新製品の販売について事業提携を発表しました。'
        item=dict(provider='edinet',source_key='doc1',title='事業提携',url='https://example.org/doc1',
            published_at=datetime.combine(START,time(12),JST),body=body,content={'matched':True,'body_state':'本文取得済み'})
        save_source(self.db,'10000',item,NOW)
        llm=FakeLLM()
        # Explicitly index a synthetic source, using real pgvector.
        from jp_screening.analysis.research import index_pending
        index_pending(self.db,llm)
        with patch.object(llm,'chat',side_effect=ScreeningError('stopped')):
            automatic_research(self.db,self.config,NOW,ollama=llm)
        self.assertTrue(any(r['state']=='waiting_model' for r in self.rows(s.monitor_analyses)))
        def reply(messages,schema=None):
            data=json.loads(messages[-1]['content'])
            return json.dumps({'factors':[{'source_id':data['sources'][0]['source_id'],'quote':body,'category':'事業・提携'}]},ensure_ascii=False)
        with patch.object(llm,'chat',side_effect=reply):
            automatic_research(self.db,self.config,NOW+timedelta(minutes=15),ollama=llm)
        complete=[r for r in self.rows(s.monitor_analyses) if r['kind']=='individual' and r['state']=='complete']
        self.assertTrue(complete[0]['content']['factors'])
        self.assertEqual(len(self.rows(s.monitor_sources)),1)

    def test_free_collectors_deduplicate_and_key_missing_is_not_a_core_error(self):
        self.load_ended()
        class FakeProviders:
            def master(self,day): return [{'Code':'10000','CoName':'合成企業','CoNameEn':'Synthetic Company'}]
            def financial(self,*args): return []
            def edinet(self,*args): raise ScreeningError('waiting key')
            def news(self,*args): return [dict(provider='gdelt',source_key='u',title='合成企業の株価',url='https://example.org/u',
                body='',published_at=None,content={'matched':True,'body_state':'本文未取得'})]
        first=collect(self.db,self.config,NOW,providers=FakeProviders())
        second=collect(self.db,self.config,NOW,providers=FakeProviders())
        self.assertEqual(first,second)
        self.assertEqual(len(self.rows(s.monitor_sources)),1)
        self.assertIn('waiting_key',{r['state'] for r in first})

    def test_readonly_dashboard_has_fixed_views_and_no_analysis_controls(self):
        from streamlit.testing.v1 import AppTest
        self.load_ended()
        automatic_research(self.db,self.config,NOW,ollama=FakeLLM())
        with patch.dict(os.environ,{'ANALYSIS_DATABASE_URL':TEST_URL,'ANALYSIS_READER_URL':TEST_URL}):
            app=AppTest.from_file(str(Path(__file__).parents[1]/'src/jp_screening/analysis/ui.py'),default_timeout=20).run()
        self.assertFalse(app.exception)
        self.assertFalse(app.error,[item.value for item in app.error])
        self.assertEqual(len(app.tabs),4)
        self.assertFalse(app.button)
        self.assertFalse(app.chat_input)
        self.assertFalse(app.sidebar.radio)
        self.assertTrue(any('要因を特定できない' in str(x.value) for x in app.markdown))

    def test_daily_screening_requests_stage_two_without_user_actions(self):
        from jp_screening.analysis.pipeline import daily_update
        import_run(self.db,run_folder(self.root))
        def screen(root,mode,key,stage):
            self.assertEqual(stage,2)
            return run_folder(root,'automatic',day=NOW.date(),results=[result(day=NOW.date())]),{}
        with patch('jp_screening.analysis.pipeline.run_live',side_effect=screen) as called:
            first=daily_update(self.config,self.db,True,NOW,FakeAPI())
            second=daily_update(self.config,self.db,True,NOW,FakeAPI())
        self.assertEqual(called.call_count,1)
        self.assertTrue(first['complete'])
        self.assertTrue(second['complete'])
        self.assertTrue(self.rows(s.monitor_periods))
        self.assertTrue(self.rows(s.monitor_analyses))

    def test_down_and_flat_exit_classification(self):
        self.load_ended(100)
        self.assertEqual(self.rows(s.monitor_periods)[0]['direction'],'flat')
        raw=quotes(days=DAYS[:2]);raw[-1]['C']=90
        save_prices(self.db,'10000',raw,NOW)
        rebuild(self.db,datetime.combine(DAYS[1],time(19),JST))
        self.assertEqual(bucket(self.rows(s.monitor_periods)[0]),'下落して通過終了')

    def test_same_day_replacement_keeps_obsolete_records_inactive(self):
        import_run(self.db,run_folder(self.root))
        rebuild(self.db,NOW)
        import_run(self.db,run_folder(self.root,'later',hour=19,
            results=[result(flags=('通過','不通過','未対象'))]))
        rebuild(self.db,NOW)
        self.assertFalse(any(r['active'] for r in self.rows(s.monitor_periods)))
        self.assertTrue(self.rows(s.monitor_periods))
        self.assertFalse(any(r['active'] for r in self.rows(s.monitor_days)))
        self.assertTrue(self.rows(s.tracking))

    def test_news_outage_uses_one_global_backoff_for_many_securities(self):
        self.load_ended()
        with self.db.transaction() as conn:
            period=self.rows(s.monitor_periods)[0]
            for code in ('20000','30000'):
                conn.execute(s.monitor_periods.insert().values(**{**period,'id':code,'code':code,'analysis_id':None}))
        class FailedNews:
            calls=0
            def master(self,day): return []
            def financial(self,*args): return []
            def edinet(self,*args): raise ScreeningError('key missing')
            def news(self,*args):
                self.calls+=1
                raise ScreeningError('HTTP 429')
        providers=FailedNews()
        collect(self.db,self.config,NOW,providers=providers)
        collect(self.db,self.config,NOW+timedelta(minutes=15),providers=providers)
        self.assertEqual(providers.calls,1)

    def test_late_key_setup_retries_same_day(self):
        from dataclasses import replace
        self.load_ended()
        class Providers:
            calls=0
            def master(self,day): return []
            def financial(self,*args): return []
            def edinet(self,*args):
                self.calls+=1
                if self.calls==1: raise ScreeningError('key missing')
                return []
            def news(self,*args): return []
        providers=Providers()
        collect(self.db,self.config,NOW,providers=providers)
        collect(self.db,replace(self.config,edinet_api_key='test'),NOW,providers=providers)
        self.assertEqual(providers.calls,2)
        self.assertIn('no_data',{r['state'] for r in self.rows(s.monitor_fetches) if r['provider']=='edinet'})

    def test_data_preserving_migration_from_v001(self):
        from uuid import uuid4
        name='monitor_migration_'+uuid4().hex+'_test'
        with self.db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
            conn.execute(text('CREATE DATABASE '+name))
        temp=Database(make_url(TEST_URL).set(database=name).render_as_string(hide_password=False))
        try:
            with temp.transaction() as conn:
                conn.execute(text('CREATE EXTENSION vector'))
                original=[t for t in s.metadata.sorted_tables if not t.name.startswith('monitor_')]
                s.metadata.create_all(conn,tables=original)
                conn.execute(text("CREATE TABLE analysis_schema_version (version_num varchar(32) primary key)"))
                conn.execute(text("INSERT INTO analysis_schema_version VALUES ('analysis_001')"))
                conn.execute(text("INSERT INTO analysis_jobs VALUES ('saved',now(),'{}')"))
            temp.upgrade()
            with temp.transaction(readonly=True) as conn:
                self.assertEqual(conn.execute(text("SELECT count(*) FROM analysis_jobs")).scalar_one(),1)
                self.assertEqual(conn.execute(text("SELECT version_num FROM analysis_schema_version")).scalar_one(),'analysis_002')
                self.assertEqual(conn.execute(text("SELECT count(*) FROM monitor_periods")).scalar_one(),0)
        finally:
            temp.engine.dispose()
            with self.db.engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
                conn.execute(text('DROP DATABASE '+name))
