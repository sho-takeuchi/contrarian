from datetime import date, datetime, timedelta
from decimal import Decimal
import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from jp_screening.api import Client
from jp_screening.core import JST, ScreeningError, TradingCalendar, adjusted_bars, evaluate
from jp_screening.demo import fixture_data, run_demo
from jp_screening.listings import parse_date, prepare_listings, read_listings, load_or_prepare
from jp_screening.reporting import write_results
from jp_screening.runner import (context, eligible, initialize, replay, repair_history,
    run_live, sync_bulk, choose_sample)
from jp_screening.storage import Store

ROOT = Path(__file__).resolve().parents[1]


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'data', prefix='test_')
        self.root = Path(self.temp.name)
        initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()


class FakeResponse:
    def __init__(self, status=200, value=None, headers=None):
        self.status_code=status
        self.text=json.dumps(value if value is not None else {'data':[]})
        self.headers=headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses=iter(responses)
        self.calls=[]

    def get(self,*args,**kwargs):
        self.calls.append((args,kwargs))
        result=next(self.responses)
        if isinstance(result,Exception):
            raise result
        return result


class ApiTests(unittest.TestCase):
    def client(self,responses):
        session=FakeSession(responses)
        sleeps=[]
        return Client('TEST_SECRET',session=session,sleep=sleeps.append),session,sleeps

    def test_pagination(self):
        client,session,_=self.client([FakeResponse(value={'data':[{'a':1}],'pagination_key':'next'}),
                                     FakeResponse(value={'data':[{'a':2}]})])
        self.assertEqual(list(client.rows('/test')),[{'a':1},{'a':2}])
        self.assertEqual(session.calls[1][1]['params']['pagination_key'],'next')

    def test_bulk_list_endpoint_query_with_pagination(self):
        client,session,_=self.client([
            FakeResponse(value={'data':[{'Key':'one'}],'pagination_key':'next'}),
            FakeResponse(value={'data':[{'Key':'two'}]})])
        rows=list(client.rows('/bulk/list',endpoint='/equities/bars/daily',
                              **{'from':'2016-10-22','to':'2026-10-22'}))
        self.assertEqual(rows,[{'Key':'one'},{'Key':'two'}])
        for args,kwargs in session.calls:
            self.assertEqual(args[0],'https://api.jquants.com/v2/bulk/list')
            self.assertEqual(kwargs['params']['endpoint'],'/equities/bars/daily')
            self.assertEqual(kwargs['params']['from'],'2016-10-22')
            self.assertEqual(kwargs['params']['to'],'2026-10-22')
        self.assertEqual(session.calls[1][1]['params']['pagination_key'],'next')

    def test_bulk_get_accepts_endpoint_query(self):
        client,session,_=self.client([FakeResponse(value={'url':'https://example.com/file.gz'})])
        response=client.get('/bulk/get',endpoint='/equities/bars/daily',date='2026-10')
        self.assertEqual(response['url'],'https://example.com/file.gz')
        self.assertEqual(session.calls[0][0][0],'https://api.jquants.com/v2/bulk/get')
        self.assertEqual(session.calls[0][1]['params'],
                         {'endpoint':'/equities/bars/daily','date':'2026-10'})

    def test_pagination_loop(self):
        client,_,_=self.client([FakeResponse(value={'data':[],'pagination_key':'x'})]*2)
        with self.assertRaises(ScreeningError):
            list(client.rows('/test'))

    def test_retry_429_500_timeout_then_success(self):
        client,session,sleeps=self.client([FakeResponse(429,headers={'Retry-After':'3'}),
            FakeResponse(503),requests.Timeout('DO_NOT_PRINT'),FakeResponse()])
        self.assertEqual(client.get('/test'),{'data':[]})
        self.assertEqual(len(session.calls),4)
        self.assertEqual(sleeps,[3,2,4])

    def test_retries_exhausted(self):
        client,session,_=self.client([FakeResponse(500)]*4)
        with self.assertRaises(ScreeningError) as caught:
            client.get('/test')
        self.assertNotIn('TEST_SECRET',str(caught.exception))
        self.assertEqual(len(session.calls),4)

    def test_auth_does_not_retry(self):
        client,session,_=self.client([FakeResponse(403)])
        with self.assertRaises(ScreeningError):
            client.get('/test')
        self.assertEqual(len(session.calls),1)

    def test_authenticated_redirects_are_rejected_without_retry_or_url_disclosure(self):
        for status in (301,302,303,307,308):
            with self.subTest(status=status):
                client,session,sleeps=self.client([FakeResponse(status,
                    headers={'Location':'https://untrusted.invalid/?secret=DO_NOT_PRINT'})])
                with self.assertRaisesRegex(ScreeningError,'リダイレクト') as caught:
                    client.get('/equities/master')
                self.assertIs(session.calls[0][1]['allow_redirects'],False)
                self.assertEqual(len(session.calls),1)
                self.assertEqual(sleeps,[])
                self.assertNotIn('TEST_SECRET',str(caught.exception))
                self.assertNotIn('DO_NOT_PRINT',str(caught.exception))
                self.assertNotIn('untrusted.invalid',str(caught.exception))

    def test_real_requests_session_never_sends_key_to_redirect_destination(self):
        class RedirectAdapter(requests.adapters.BaseAdapter):
            def __init__(self): self.destinations=[]
            def send(self,request,**kwargs):
                self.destinations.append(request.url)
                response=requests.Response()
                response.status_code=302
                response.headers['Location']='https://untrusted.invalid/collect'
                response.request=request
                response.url=request.url
                response._content=b''
                return response
            def close(self): pass
        adapter=RedirectAdapter()
        with requests.Session() as session:
            session.trust_env=False
            session.mount('https://',adapter)
            with self.assertRaisesRegex(ScreeningError,'リダイレクト'):
                Client('AUDIT_DUMMY_NOT_REAL',session=session).get('/equities/master')
        self.assertEqual(adapter.destinations,['https://api.jquants.com/v2/equities/master'])

    def test_no_secret_in_connection_error(self):
        client,_,_=self.client([requests.ConnectionError('TEST_SECRET')]*4)
        with self.assertRaises(ScreeningError) as caught:
            client.get('/test')
        self.assertNotIn('TEST_SECRET',str(caught.exception))

    def test_rate_limit_sliding_window(self):
        clock=[0.0]
        def sleep(seconds):
            clock[0]+=seconds
        client=Client('x',session=FakeSession([FakeResponse()]*91),clock=lambda:clock[0],sleep=sleep)
        for _ in range(91):
            client.get('/test')
        self.assertGreaterEqual(clock[0],60)

    def test_provider_decimal_is_preserved(self):
        client,_,_=self.client([FakeResponse(value={'data':[{'C':0.1}]})])
        self.assertEqual(client.get('/test')['data'][0]['C'],Decimal('0.1'))


class StorageTests(TemporaryProject):
    def setUp(self):
        super().setUp()
        self.store=Store(self.root/'data'/'prices.sqlite3')
        self.row=dict(Date='2026-10-21',Code='10000',O='100',H='110',L='90',C='100',AdjFactor='1')

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_transaction_rollback_leaves_previous_data(self):
        self.store.replace_source('test',[self.row],('test','one',1,'abc'))
        with self.assertRaises(ScreeningError):
            self.store.replace_source('test',[{**self.row,'C':'200'},self.row],('test','two',2,'def'))
        self.assertEqual(self.store.prices('10000',date(2026,1,1),date(2026,12,31))[0]['C'],'100')
        self.assertTrue(self.store.file_current(dict(Key='test',LastModified='one',Size=1)))

    def test_changed_file_version_replaced(self):
        self.store.replace_source('test',[self.row],('test','one',1,'abc'))
        self.assertFalse(self.store.file_current(dict(Key='test',LastModified='two',Size=1)))
        self.store.replace_source('test',[{**self.row,'C':'200'}],('test','two',1,'def'))
        self.assertEqual(self.store.prices('10000',date(2026,1,1),date(2026,12,31))[0]['C'],'200')

    def test_resume_skips_completed_download(self):
        item=dict(Key='done',LastModified='one',Size=1)
        self.store.replace_source('done',[self.row],('done','one',1,'abc'))
        class Fake:
            def rows(self,*a,**kw): return iter([item])
            def get(self,*a,**kw): raise AssertionError('completed file downloaded twice')
        sync_bulk(Fake(),self.store,self.root,date(2016,10,21),date(2026,10,21))

    def test_gzip_import_and_size_validation(self):
        path=self.root/'data'/'one.gz'
        with gzip.open(path,'wt',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(self.row))
            writer.writeheader()
            writer.writerow(self.row)
        item=dict(Key='one',LastModified='today',Size=path.stat().st_size)
        self.store.import_gzip(item,path)
        self.assertTrue(self.store.file_current(item))
        with self.assertRaises(ScreeningError):
            self.store.import_gzip({**item,'Size':1},path)

    def test_bulk_sync_real_client_transport_and_resume(self):
        path=self.root/'data'/'fixture.csv.gz'
        with gzip.open(path,'wt',encoding='utf-8',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(self.row))
            writer.writeheader()
            writer.writerow(self.row)
        item=dict(Key='historical/2026/prices.csv.gz',LastModified='2026-10-22T08:00:00Z',
                  Size=path.stat().st_size)
        session=FakeSession([FakeResponse(value={'data':[item]}),
                             FakeResponse(value={'url':'https://example.com/fixture.csv.gz'}),
                             FakeResponse(value={'data':[item]})])
        client=Client('TEST_SECRET',session=session)
        def download(url,destination):
            self.assertEqual(url,'https://example.com/fixture.csv.gz')
            destination.write_bytes(path.read_bytes())
        with patch.object(client,'download',side_effect=download) as mocked_download:
            sync_bulk(client,self.store,self.root,date(2016,10,22),date(2026,10,22))
            sync_bulk(client,self.store,self.root,date(2016,10,22),date(2026,10,22))
            self.assertEqual(mocked_download.call_count,1)
        self.assertTrue(self.store.file_current(item))
        cached=self.store.prices('10000',date(2016,10,22),date(2026,10,22))
        self.assertEqual(cached[0]['C'],'100')
        self.assertEqual([call[0][0].rsplit('/',1)[-1] for call in session.calls],
                         ['list','get','list'])

    def test_missing_record_repaired_null_is_not_repaired(self):
        cal=TradingCalendar.from_rows([dict(Date='2026-10-21',HolDiv='1')])
        class Fake:
            def daily(inner,**kw): return [self.row]
        rows=repair_history(Fake(),self.store,'10000',date(2026,10,21),date(2026,10,21),cal,date(2010,1,1))
        self.assertEqual(len(rows),1)
        null={**self.row,'C':None,'O':None,'H':None,'L':None}
        self.store.replace_source('null',[null])
        class NoFetch:
            def daily(inner,**kw): raise AssertionError('Null must not be filled')
        self.assertIsNone(repair_history(NoFetch(),self.store,'10000',date(2026,10,21),date(2026,10,21),cal,date(2010,1,1))[0]['C'])

    def test_missing_after_retry_rejected(self):
        cal=TradingCalendar.from_rows([dict(Date='2026-10-21',HolDiv='1')])
        class Fake:
            def daily(inner,**kw): return []
        with self.assertRaises(ScreeningError):
            repair_history(Fake(),self.store,'10000',date(2026,10,21),date(2026,10,21),cal,date(2010,1,1))


class ListingTests(TemporaryProject):
    def test_reuse_preserves_original_provenance(self):
        source=self.root/'inputs'/'official.csv'
        source.write_text('Code,listing_date\n1234,2010-01-04\n',encoding='utf-8')
        prepare_listings(self.root/'inputs',source)
        provenance=self.root/'inputs'/'listing_dates.source.json'
        original=provenance.read_bytes()
        load_or_prepare(self.root/'inputs')
        self.assertEqual(provenance.read_bytes(),original)

    def test_official_japanese_headers_and_provenance(self):
        path=self.root/'inputs'/'official.csv'
        path.write_text('銘柄コード,東証上場日\n001A,2018/10/21\n1234,2010年1月4日\n',encoding='cp932')
        values=prepare_listings(self.root/'inputs',path)
        self.assertEqual(values['001A0'],date(2018,10,21))
        metadata=json.loads((self.root/'inputs'/'listing_dates.source.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['entries'],2)
        self.assertTrue(metadata['sha256'])

    def test_missing_date_not_inferred(self):
        path=self.root/'inputs'/'official.csv'
        path.write_text('Code,listing_date\n1234,\n',encoding='utf-8-sig')
        self.assertIsNone(read_listings(path)['12340'])

    def test_conflicting_listing_dates_rejected(self):
        path=self.root/'inputs'/'official.csv'
        path.write_text('Code,listing_date\n1234,20100104\n1234,20110104\n',encoding='utf-8')
        with self.assertRaises(ScreeningError): read_listings(path)

    def test_bad_header_keeps_good_input(self):
        target=self.root/'inputs'/'listing_dates.csv'
        target.write_text('Code,listing_date\n1234,20100104\n',encoding='utf-8')
        bad=self.root/'inputs'/'bad.csv'
        bad.write_text('wrong,header\n1,2\n',encoding='utf-8')
        with self.assertRaises(ScreeningError): prepare_listings(self.root/'inputs',bad)
        self.assertEqual(read_listings(target)['12340'],date(2010,1,4))


class IntegrationTests(TemporaryProject):
    def test_legacy_replay_and_current_rules_recompute(self):
        cal,calendar_rows,asof,start,data=fixture_data()
        month_end=str(cal.month_end(date(2026,9,1)))
        for row in data[0][2]:
            if row['Date']==month_end:
                row.update(O=None,H=None,L=None,C=None)
        def legacy_evaluate(*args,**kwargs):
            return evaluate(*args,**kwargs,close_policy='month_end')
        with patch('jp_screening.demo.fixture_data',return_value=(cal,calendar_rows,asof,start,data)), \
             patch('jp_screening.demo.evaluate',side_effect=legacy_evaluate):
            folder,manifest=run_demo(self.root)
        manifest.pop('settings',None)  # Format used before this change.
        (folder/'run.json').write_text(json.dumps(manifest,ensure_ascii=False,default=str),encoding='utf-8')
        replay_folder,_=replay(self.root,manifest['run_id'])
        self.assertTrue(json.loads((replay_folder/'replay.json').read_text(encoding='utf-8'))['identical'])
        new_folder,summary=replay(self.root,manifest['run_id'],current_rules=True)
        self.assertEqual(summary['通過'],manifest['counts']['通過']+1)
        changes=json.loads((new_folder/'change_summary.json').read_text(encoding='utf-8'))
        self.assertEqual(changes['transitions']['判定不能 → 通過'],1)
        with (new_folder/'monthly_audit.csv').open(encoding='utf-8-sig',newline='') as f:
            selected=[r for r in csv.DictReader(f) if r['銘柄コード']=='10000' and r['月']=='2026-09'][0]
        self.assertEqual(selected['終値採用日'],'2026-09-29')
        self.assertEqual(selected['月末との差_営業日'],'1')
        again,_=replay(self.root,new_folder.name)
        self.assertTrue(json.loads((again/'replay.json').read_text(encoding='utf-8'))['identical'])

    def test_demo_and_replay_identical(self):
        folder,manifest=run_demo(self.root)
        self.assertTrue(manifest['synthetic'])
        self.assertEqual(manifest['counts']['通過'],1)
        self.assertEqual(manifest['counts']['不通過'],2)
        self.assertEqual(manifest['counts']['判定不能'],1)
        replay_folder,summary=replay(self.root,manifest['run_id'])
        self.assertTrue((replay_folder/'SYNTHETIC_DATA.txt').exists())
        self.assertTrue(json.loads((replay_folder/'replay.json').read_text(encoding='utf-8'))['synthetic'])
        for name in ('candidates.csv','all_results.csv','monthly_audit.csv'):
            self.assertEqual((folder/name).read_bytes(),(replay_folder/name).read_bytes())
            self.assertTrue((folder/name).read_bytes().startswith(b'\xef\xbb\xbf'))

    def test_replay_rejects_tamper(self):
        folder,manifest=run_demo(self.root)
        with Path(manifest['snapshot']).open('ab') as f: f.write(b'x')
        with self.assertRaises(ScreeningError): replay(self.root,manifest['run_id'])

    def test_universe_filter(self):
        stock=dict(Code='12340',ProdCat='011',Mkt='0111')
        self.assertTrue(eligible(stock))
        self.assertTrue(eligible(dict(Code='89510',ProdCat='013',Mkt='0109')))
        for change in (dict(Code='12345'),dict(Mkt='0109'),dict(ProdCat='014'),dict(ProdCat='021')):
            self.assertFalse(eligible({**stock,**change}))

    def test_waiting_for_publication_does_not_fallback(self):
        _,calendar_rows,*_=fixture_data()
        class Fake:
            def rows(self,*a,**kw): return iter(calendar_rows)
            def daily(self,**kw): return []
        with self.assertRaisesRegex(ScreeningError,'更新待ち'):
            context(Fake(),datetime(2026,10,21,18,tzinfo=JST))

    def test_sample_and_all_with_fake_transport(self):
        cal,calendar_rows,asof,start,data=fixture_data()
        # Real HTTP is replaced at the boundary; aggregation, SQLite, exports,
        # verification gate and replay execute normally.
        rows_by_code={m['Code']: rows for m,listing,rows in data}
        listings=self.root/'inputs'/'official.csv'
        listings.write_text('Code,listing_date\n'+'\n'.join(m['Code']+',2010-01-04' for m,_,_ in data),encoding='utf-8')
        class FakeClient:
            def __init__(self,*a): pass
            def rows(self,path,**kw):
                return iter(calendar_rows if path=='/markets/calendar' else [m for m,_,_ in data])
            def daily(self,code=None,start=None,end=None,day=None):
                chosen=rows_by_code.get(code,[]) if code else sum(rows_by_code.values(),[])
                chosen=[dict(r) for r in chosen if (not day or r['Date']==str(day)) and
                        (not start or r['Date']>=str(start)) and (not end or r['Date']<=str(end))]
                for r in chosen:
                    for field in ('O','H','L','C'): r['Adj'+field]=r[field]
                return chosen
        def sync(client,store,root,start,end):
            store.replace_source('fixture',[r for rows in rows_by_code.values() for r in rows if r['Date']>=str(start)])
            return []
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return cls(2026,10,21,18,0,tzinfo=JST)
        with patch('jp_screening.runner.Client',FakeClient),patch('jp_screening.runner.sync_bulk',sync),patch('jp_screening.runner.datetime',Clock):
            _,sample_manifest=run_live(self.root,'sample','FAKE',listing_file=listings)
            folder,manifest=run_live(self.root,'all','FAKE')
            weekly_folder,weekly_manifest=run_live(self.root,'all','FAKE',stage=2)
            daily_folder,daily_manifest=run_live(self.root,'all','FAKE',stage=3)
        self.assertEqual(manifest['counts']['total'],4)
        self.assertTrue(all(v['price_match'] and v['reference_match'] for v in manifest['validation']))
        self.assertEqual(weekly_manifest['counts']['stage1_pass'],1)
        self.assertEqual(weekly_manifest['counts']['stage2_total'],1)
        self.assertTrue((weekly_folder/'weekly_audit.csv').exists())
        self.assertEqual(daily_manifest['settings']['stage'],3)
        self.assertEqual(daily_manifest['counts']['stage3_total'],weekly_manifest['counts']['stage2_pass'])
        self.assertTrue((daily_folder/'daily_results.csv').exists())
        daily_replay,_=replay(self.root,daily_manifest['run_id'])
        self.assertEqual((daily_folder/'all_results.csv').read_bytes(),(daily_replay/'all_results.csv').read_bytes())
        weekly_replay,_=replay(self.root,weekly_manifest['run_id'])
        self.assertEqual((weekly_folder/'all_results.csv').read_bytes(),(weekly_replay/'all_results.csv').read_bytes())
        replay_folder,_=replay(self.root,manifest['run_id'])
        self.assertEqual((folder/'all_results.csv').read_bytes(),(replay_folder/'all_results.csv').read_bytes())

    def test_failed_validation_prevents_all_scan(self):
        cal,calendar_rows,asof,start,data=fixture_data()
        meta,listing,rows=data[0]
        source=self.root/'inputs'/'official.csv'
        source.write_text('Code,listing_date\n10000,2010-01-04\n',encoding='utf-8')
        mismatch=[dict(code='10000',price_match=False,reference_match=True)]
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return cls(2026,10,21,18,tzinfo=JST)
        with patch('jp_screening.runner.context',return_value=(cal,calendar_rows,asof,[],[meta])), \
             patch('jp_screening.runner.sync_bulk',return_value=[]), \
             patch('jp_screening.runner.validate_sample',return_value=(mismatch,{})), \
             patch('jp_screening.runner.datetime',Clock):
            with self.assertRaisesRegex(ScreeningError,'少数照合が不一致'):
                run_live(self.root,'all','FAKE',listing_file=source)
        self.assertFalse(list((self.root/'outputs').rglob('candidates.csv')))
        self.assertEqual(len(list((self.root/'outputs').rglob('error.json'))),1)


if __name__=='__main__': unittest.main()
