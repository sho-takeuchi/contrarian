from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jp_screening.core import Bar, TradingCalendar
from jp_screening.daily import daily_closes, evaluate_daily, reference_check_daily
from jp_screening.demo import fixture_data, run_demo
from jp_screening.runner import initialize, replay
from jp_screening.cli import main

ROOT = Path(__file__).resolve().parents[1]


def fixture():
    days = {}
    day = date(2026,10,1)
    while day <= date(2026,10,31):
        days[day] = '1' if day.weekday() < 5 else '0'
        day += timedelta(days=1)
    cal = TradingCalendar(days)
    asof = date(2026,10,22)
    sessions = cal.sessions(date(2026,10,1),asof)
    values = dict(zip(reversed(sessions[-6:]), map(D, (90,100,100,100,100,110))))
    bars = [Bar(d,'10000',values.get(d,D(200)),None,None) for d in sessions]
    stage2 = dict(code='10000',name='合成',status='通過',reason='',
                  stage1_status='通過',stage2_status='通過',stage2_reason='')
    return bars, cal, asof, stage2


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.bars,self.cal,self.asof,self.stage2 = fixture()

    def calc(self,bars=None,cal=None):
        return evaluate_daily(self.stage2,self.bars if bars is None else bars,
                              self.cal if cal is None else cal,self.asof)

    def test_two_conditions_with_known_values(self):
        result,days = self.calc()
        self.assertEqual(result['sma5_day'],D(98))
        self.assertEqual(result['previous_sma5_day'],D(102))
        self.assertEqual(result['status'],'通過')
        self.assertTrue(result['daily_condition_1'])
        self.assertTrue(result['daily_condition_2'])
        self.assertEqual(reference_check_daily(result,days),[])
        self.assertEqual([d.day for d in days],[date(2026,10,d) for d in (22,21,20,19,16,15)])

    def test_equality_to_average_fails_first_condition(self):
        bars = [replace(b,close=D(100)) if b.day==self.asof else b for b in self.bars]
        result,_ = self.calc(bars)
        self.assertFalse(result['daily_condition_1'])
        self.assertTrue(result['daily_condition_2'])
        self.assertEqual(result['status'],'不通過')

    def test_equal_averages_fail_second_condition(self):
        bars = [replace(b,close=D(90)) if b.day==date(2026,10,15) else b for b in self.bars]
        result,_ = self.calc(bars)
        self.assertTrue(result['daily_condition_1'])
        self.assertFalse(result['daily_condition_2'])
        self.assertEqual(result['status'],'不通過')

    def test_above_average_can_have_downward_average(self):
        bars = [replace(b,close=D(110)) if b.day==self.asof else
                replace(b,close=D(130)) if b.day==date(2026,10,15) else b for b in self.bars]
        result,_ = self.calc(bars)
        self.assertFalse(result['daily_condition_1'])
        self.assertTrue(result['daily_condition_2'])
        self.assertEqual(result['status'],'不通過')

    def test_below_average_can_have_upward_average(self):
        bars = [replace(b,close=D(80)) if b.day==date(2026,10,15) else b for b in self.bars]
        result,_ = self.calc(bars)
        self.assertTrue(result['daily_condition_1'])
        self.assertFalse(result['daily_condition_2'])

    def test_future_day_and_intraday_quotes_excluded(self):
        bars = self.bars + [Bar(date(2026,10,23),'10000',D(9999),None,None)]
        self.assertEqual(self.calc(bars),self.calc())

    def test_null_latest_is_not_filled(self):
        bars = [replace(b,close=None) if b.day==self.asof else b for b in self.bars]
        result,days = self.calc(bars)
        self.assertIsNone(result['daily_condition_1'])
        self.assertIsNone(result['daily_condition_2'])
        self.assertTrue(days[0].record_present)
        self.assertEqual(result['status'],'判定不能')

    def test_missing_record_keeps_its_business_day_slot(self):
        result,days = self.calc([b for b in self.bars if b.day!=date(2026,10,20)])
        self.assertEqual(days[2].day,date(2026,10,20))
        self.assertFalse(days[2].record_present)
        self.assertIsNone(days[2].close)
        self.assertEqual(len(days),6)
        self.assertEqual(result['status'],'判定不能')

    def test_sixth_close_only_affects_average_direction(self):
        bars = [replace(b,close=None) if b.day==date(2026,10,15) else b for b in self.bars]
        result,_ = self.calc(bars)
        self.assertTrue(result['daily_condition_1'])
        self.assertIsNone(result['daily_condition_2'])
        self.assertEqual(result['status'],'判定不能')

    def test_ose_only_holiday_and_holiday_are_skipped(self):
        for kind in ('0','3'):
            days = {**self.cal.days,date(2026,10,19):kind}
            actual = daily_closes(self.bars,TradingCalendar(days),self.asof)
            self.assertNotIn(date(2026,10,19),[d.day for d in actual])
            self.assertEqual(actual[-1].day,date(2026,10,14))

    def test_calendar_gap_is_unknown(self):
        days = dict(self.cal.days)
        del days[date(2026,10,20)]
        result,audit = self.calc(cal=TradingCalendar(days))
        self.assertEqual(result['status'],'判定不能')
        self.assertEqual(audit,[])

    def test_insufficient_calendar_is_unknown(self):
        days = {d:k for d,k in self.cal.days.items() if d>=date(2026,10,19)}
        self.assertEqual(self.calc(cal=TradingCalendar(days))[0]['status'],'判定不能')

    def test_non_business_asof_is_unknown(self):
        result,_ = evaluate_daily(self.stage2,self.bars,self.cal,date(2026,10,24))
        self.assertEqual(result['status'],'判定不能')

    def test_only_stage_two_passes_are_evaluated(self):
        for status in ('不通過','判定不能','未対象'):
            stage2 = {**self.stage2,'status':'不通過','stage2_status':status}
            result,days = evaluate_daily(stage2,[],TradingCalendar({}),self.asof)
            self.assertEqual(result['status'],'不通過')
            self.assertEqual(result['stage3_status'],'未対象')
            self.assertEqual(days,[])

    def test_independent_reference_detects_incorrect_flags(self):
        result,days = self.calc()
        result['daily_condition_1'] = False
        result['daily_condition_2'] = False
        self.assertEqual(reference_check_daily(result,days),['daily_condition_1','daily_condition_2'])


class DailyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'data',prefix='test_daily_')
        self.root = Path(self.temp.name)
        initialize(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def saved_fixture(self):
        cal,calendar,asof,start,data = fixture_data()
        meta,listing,rows = data[0]
        for row in rows:
            day = date.fromisoformat(row['Date'])
            value = D(140) if date(2026,10,12)<=day<=date(2026,10,16) else {
                date(2026,10,19):D(138),date(2026,10,20):D(135),date(2026,10,21):D(130)}.get(day)
            if value is not None:
                row.update(O=str(value),H=str(value+2),L=str(value-2),C=str(value))
        with patch('jp_screening.demo.fixture_data',return_value=(cal,calendar,asof,start,[(meta,listing,rows)])):
            return run_demo(self.root)

    def test_three_stages_exports_replay_and_return_to_stage_two(self):
        _,original = self.saved_fixture()
        stage2,old_counts = replay(self.root,original['run_id'],current_rules=True,stage=2)
        self.assertEqual(old_counts['stage2_pass'],1)
        stage3,summary = replay(self.root,stage2.name,current_rules=True,stage=3)
        self.assertEqual(summary['stage3_total'],1)
        self.assertEqual(summary['stage3_pass'],1)
        self.assertEqual(summary['stage3_unknown'],0)
        saved = json.loads((stage3/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['settings']['stage'],3)
        self.assertEqual(saved['daily_reference_checks'],{'records':1,'errors':0})
        with (stage3/'daily_results.csv').open(encoding='utf-8-sig',newline='') as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['第3段階判定'],'通過')
        with (stage3/'daily_audit.csv').open(encoding='utf-8-sig',newline='') as f:
            self.assertEqual(len(list(csv.DictReader(f))),6)
        again,_ = replay(self.root,stage3.name)
        for name in ('all_results.csv','candidates.csv','monthly_audit.csv','weekly_audit.csv',
                     'daily_results.csv','daily_audit.csv','stage2_candidates.csv'):
            self.assertEqual((stage3/name).read_bytes(),(again/name).read_bytes())
        back,summary = replay(self.root,stage3.name,current_rules=True,stage=2)
        self.assertEqual(summary,old_counts)
        self.assertEqual((back/'all_results.csv').read_bytes(),(stage2/'all_results.csv').read_bytes())
        back_manifest = json.loads((back/'run.json').read_text(encoding='utf-8'))
        self.assertNotIn('daily',back_manifest['settings'])
        self.assertNotIn('daily_reference_checks',back_manifest)
        self.assertFalse((back/'daily_results.csv').exists())

    def test_daily_cli_requires_no_key_and_keeps_saved_asof(self):
        _,manifest = self.saved_fixture()
        with patch.dict(os.environ,{},clear=True),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(['daily','--run-id',manifest['run_id'],'--root',str(self.root)]),0)
        output = next((self.root/'outputs').glob('*_stage3'))
        saved = json.loads((output/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['asof'],'2026-10-21')
        self.assertEqual(saved['counts']['stage3_pass'],1)


if __name__ == '__main__':
    unittest.main()
