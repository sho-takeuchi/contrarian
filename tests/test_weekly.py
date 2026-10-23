from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jp_screening.core import Bar,TradingCalendar
from jp_screening.weekly import weekly_bars,evaluate_weekly,reference_check_weekly
from jp_screening.runner import initialize,replay
from jp_screening.demo import run_demo
from jp_screening.cli import main

ROOT=Path(__file__).resolve().parents[1]


def fixture():
    days={}
    day=date(2026,8,24)
    end=date(2026,10,30)
    while day<=end:
        days[day]='1' if day.weekday()<5 else '0'
        day+=timedelta(days=1)
    cal=TradingCalendar(days)
    values={date(2026,9,7):100,date(2026,9,14):100,date(2026,9,21):102,
            date(2026,9,28):104,date(2026,10,5):108,date(2026,10,12):120,
            date(2026,10,19):9999}
    bars=[]
    for day in cal.sessions(date(2026,8,24),end):
        monday=day-timedelta(days=day.weekday())
        close=D(values.get(monday,80))
        bars.append(Bar(day,'10000',close,close-2,close+2,close))
    stage1=dict(code='10000',name='合成',product='株式',status='通過',reason='',
                condition_a=True,condition_b=True,condition_c=True)
    return bars,cal,stage1,date(2026,10,22)


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.bars,self.cal,self.stage1,self.asof=fixture()

    def calc(self,bars=None,cal=None,asof=None):
        return evaluate_weekly(self.stage1,self.bars if bars is None else bars,
            self.cal if cal is None else cal,self.asof if asof is None else asof,require_breakout=True)

    def test_three_conditions_pass_with_known_values(self):
        result,weeks=self.calc()
        self.assertEqual(result['stage1_status'],'通過')
        self.assertEqual(result['stage2_status'],'通過')
        self.assertEqual(result['status'],'通過')
        self.assertEqual(result['sma5_week'],D('106.8'))
        self.assertEqual(result['previous_sma5_week'],D('102.8'))
        self.assertEqual(result['previous_three_week_high'],D(110))
        self.assertEqual(reference_check_weekly(result,weeks,require_breakout=True),[])

    def test_current_incomplete_week_and_future_bars_excluded(self):
        result,weeks=self.calc()
        self.assertEqual(weeks[0].start_day,date(2026,10,12))
        self.assertEqual(weeks[0].end_day,date(2026,10,16))
        self.assertEqual(result['w1_close'],D(120))
        self.assertEqual(len(weeks),6)

    def test_friday_uses_just_completed_week(self):
        result,weeks=self.calc(asof=date(2026,10,23))
        self.assertEqual(weeks[0].end_day,date(2026,10,23))
        self.assertEqual(result['w1_close'],D(9999))

    def test_friday_holiday_completes_on_thursday(self):
        days=dict(self.cal.days)
        days[date(2026,10,23)]='0'
        result,weeks=self.calc(cal=TradingCalendar(days))
        self.assertEqual(weeks[0].start_day,date(2026,10,19))
        self.assertEqual(weeks[0].end_day,date(2026,10,22))

    def test_derivatives_only_holiday_is_not_cash_session(self):
        days=dict(self.cal.days)
        days[date(2026,10,23)]='3'
        self.assertEqual(self.calc(cal=TradingCalendar(days))[1][0].end_day,date(2026,10,22))

    def test_equality_fails_all_strict_conditions(self):
        bars=[replace(b,close=D(100),high=D(100),low=D(98),open=D(100)) for b in self.bars]
        result,_=self.calc(bars)
        self.assertEqual([result[f'weekly_condition_{i}'] for i in (1,2,3)],[False,False,False])
        self.assertEqual(result['stage2_status'],'不通過')

    def test_previous_average_uses_sixth_week(self):
        bars=[replace(b,close=D(130)) if date(2026,9,7)<=b.day<=date(2026,9,11) else b for b in self.bars]
        result,weeks=self.calc(bars)
        self.assertTrue(result['weekly_condition_1'])
        self.assertFalse(result['weekly_condition_2'])
        self.assertEqual(reference_check_weekly(result,weeks,require_breakout=True),[])

    def test_current_week_high_not_in_breakout_comparison(self):
        bars=[replace(b,high=D(999)) if date(2026,10,12)<=b.day<=date(2026,10,16) else b for b in self.bars]
        result,_=self.calc(bars)
        self.assertTrue(result['weekly_condition_3'])
        self.assertEqual(result['previous_three_week_high'],D(110))

    def test_fourth_previous_week_high_not_in_comparison(self):
        bars=[replace(b,high=D(999)) if date(2026,9,14)<=b.day<=date(2026,9,18) else b for b in self.bars]
        self.assertTrue(self.calc(bars)[0]['weekly_condition_3'])

    def test_third_previous_week_high_is_included(self):
        bars=[replace(b,high=D(150)) if b.day==date(2026,9,23) else b for b in self.bars]
        result,_=self.calc(bars)
        self.assertEqual(result['previous_three_week_high'],D(150))
        self.assertFalse(result['weekly_condition_3'])

    def test_breakout_uses_daily_highs_not_closes(self):
        bars=[replace(b,high=D(125)) if b.day==date(2026,10,7) else b for b in self.bars]
        result,_=self.calc(bars)
        self.assertEqual(result['w2_high'],D(125))
        self.assertFalse(result['weekly_condition_3'])

    def test_high_equal_to_close_is_not_breakout(self):
        bars=[replace(b,high=D(120)) if b.day==date(2026,10,7) else b for b in self.bars]
        self.assertFalse(self.calc(bars)[0]['weekly_condition_3'])

    def test_last_no_trade_day_uses_last_quote_in_week(self):
        bars=[replace(b,close=None,open=None,high=None,low=None) if b.day==date(2026,10,16) else b for b in self.bars]
        result,weeks=self.calc(bars)
        self.assertEqual(weeks[0].close_day,date(2026,10,15))
        self.assertEqual(weeks[0].no_trade_days,1)
        self.assertEqual(result['w1_close'],D(120))

    def test_whole_no_trade_week_not_skipped(self):
        bars=[replace(b,close=None,open=None,high=None,low=None)
              if date(2026,10,5)<=b.day<=date(2026,10,9) else b for b in self.bars]
        result,weeks=self.calc(bars)
        self.assertEqual(weeks[1].end_day,date(2026,10,9))
        self.assertIsNone(weeks[1].close)
        self.assertIsNone(result['weekly_condition_1'])
        self.assertEqual(result['stage2_status'],'判定不能')

    def test_exchange_wide_closed_week_skipped(self):
        days=dict(self.cal.days)
        for i in range(5): days[date(2026,10,5)+timedelta(days=i)]='0'
        _,weeks=self.calc(cal=TradingCalendar(days))
        self.assertEqual(weeks[1].end_day,date(2026,10,2))

    def test_missing_record_not_forward_filled(self):
        bars=[b for b in self.bars if b.day!=date(2026,10,14)]
        result,weeks=self.calc(bars)
        self.assertEqual(weeks[0].missing,1)
        self.assertIsNone(weeks[0].close)
        self.assertEqual(result['stage2_status'],'判定不能')

    def test_partial_ohlc_does_not_allow_previous_close(self):
        bars=[replace(b,close=None) if b.day==date(2026,10,16) else b for b in self.bars]
        self.assertIsNone(self.calc(bars)[1][0].close)

    def test_missing_sixth_close_only_affects_average_direction(self):
        bars=[replace(b,close=None,open=None,high=None,low=None)
              if date(2026,9,7)<=b.day<=date(2026,9,11) else b for b in self.bars]
        result,_=self.calc(bars)
        self.assertTrue(result['weekly_condition_1'])
        self.assertIsNone(result['weekly_condition_2'])
        self.assertTrue(result['weekly_condition_3'])

    def test_default_two_conditions_pass_without_high_breakout(self):
        bars=[replace(b,high=D(150)) if b.day==date(2026,10,7) else b for b in self.bars]
        result,weeks=evaluate_weekly(self.stage1,bars,self.cal,self.asof)
        self.assertTrue(result['weekly_condition_1'])
        self.assertTrue(result['weekly_condition_2'])
        self.assertIsNone(result['weekly_condition_3'])
        self.assertFalse(result['weekly_breakout_required'])
        self.assertEqual(result['stage2_status'],'通過')
        self.assertEqual(reference_check_weekly(result,weeks),[])
        self.assertEqual(self.calc(bars)[0]['stage2_status'],'不通過')

    def test_default_does_not_require_weekly_highs(self):
        bars=[replace(b,high=None) for b in self.bars]
        result,weeks=evaluate_weekly(self.stage1,bars,self.cal,self.asof)
        self.assertEqual(result['stage2_status'],'通過')
        self.assertIsNone(weeks[0].high)
        self.assertEqual(reference_check_weekly(result,weeks),[])
        self.assertEqual(self.calc(bars)[0]['stage2_status'],'判定不能')

    def test_default_sixth_close_still_required(self):
        bars=[replace(b,close=None,open=None,high=None,low=None)
              if date(2026,9,7)<=b.day<=date(2026,9,11) else b for b in self.bars]
        result,_=evaluate_weekly(self.stage1,bars,self.cal,self.asof)
        self.assertTrue(result['weekly_condition_1'])
        self.assertIsNone(result['weekly_condition_2'])
        self.assertEqual(result['stage2_status'],'判定不能')

    def test_default_equal_average_is_not_passed(self):
        bars=[replace(b,close=D(100),high=D(100),low=D(98),open=D(100)) for b in self.bars]
        result,_=evaluate_weekly(self.stage1,bars,self.cal,self.asof)
        self.assertFalse(result['weekly_condition_1'])
        self.assertFalse(result['weekly_condition_2'])
        self.assertEqual(result['stage2_status'],'不通過')

    def test_stage_one_failure_not_weekly_evaluated(self):
        for status in ('不通過','判定不能','対象外'):
            result,weeks=evaluate_weekly({**self.stage1,'status':status},[],TradingCalendar({}),self.asof)
            self.assertEqual(result['status'],status)
            self.assertEqual(result['stage2_status'],'未対象')
            self.assertEqual(weeks,[])


class WeeklyIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'data',prefix='test_weekly_')
        self.root=Path(self.temp.name)
        initialize(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_saved_stage_one_to_stage_two_and_replay(self):
        original,manifest=run_demo(self.root)
        folder,summary=replay(self.root,manifest['run_id'],current_rules=True,stage=2)
        self.assertEqual(summary['total'],4)
        self.assertEqual(summary['stage1_pass'],1)
        self.assertEqual(summary['stage2_total'],1)
        self.assertEqual(summary['stage2_pass'],0)
        with (folder/'weekly_results.csv').open(encoding='utf-8-sig',newline='') as f: rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['第1段階判定'],'通過')
        self.assertEqual(rows[0]['第2段階判定'],'不通過')
        saved=json.loads((folder/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['settings']['stage'],2)
        self.assertEqual(saved['weekly_reference_checks'],{'records':1,'errors':0})
        again,_=replay(self.root,folder.name)
        for name in ('all_results.csv','candidates.csv','weekly_results.csv','weekly_audit.csv'):
            self.assertEqual((folder/name).read_bytes(),(again/name).read_bytes())
        stage1_folder,stage1_summary=replay(self.root,folder.name,current_rules=True,stage=1)
        self.assertEqual(stage1_summary,manifest['counts'])
        stage1_manifest=json.loads((stage1_folder/'run.json').read_text(encoding='utf-8'))
        self.assertEqual(stage1_manifest['settings']['stage'],1)
        self.assertNotIn('weekly',stage1_manifest['settings'])
        self.assertNotIn('weekly_reference_checks',stage1_manifest)
        self.assertFalse((stage1_folder/'weekly_results.csv').exists())
        self.assertTrue(original.exists())

    def test_legacy_three_condition_result_can_still_be_replayed(self):
        from jp_screening.weekly import WEEKLY_SETTINGS
        _,manifest=run_demo(self.root)
        legacy_settings={k:v for k,v in WEEKLY_SETTINGS.items() if k!='require_breakout'}
        legacy_settings['breakout_weeks']=3
        def legacy_evaluate(*args,**kwargs):
            return evaluate_weekly(*args,**{**kwargs,'require_breakout':True})
        def legacy_reference(*args,**kwargs):
            return reference_check_weekly(*args,**{**kwargs,'require_breakout':True})
        with patch('jp_screening.runner.WEEKLY_SETTINGS',legacy_settings), \
             patch('jp_screening.runner.evaluate_weekly',side_effect=legacy_evaluate), \
             patch('jp_screening.runner.reference_check_weekly',side_effect=legacy_reference):
            old,_=replay(self.root,manifest['run_id'],current_rules=True,stage=2)
        again,old_counts=replay(self.root,old.name)
        self.assertIn('weekly_condition_3_pass',old_counts)
        for name in ('all_results.csv','candidates.csv','weekly_results.csv','weekly_audit.csv'):
            self.assertEqual((old/name).read_bytes(),(again/name).read_bytes())
        new,new_counts=replay(self.root,old.name,current_rules=True,stage=2)
        self.assertNotIn('weekly_condition_3_pass',new_counts)
        with (new/'weekly_results.csv').open(encoding='utf-8-sig',newline='') as f:
            self.assertNotIn('週足条件3_直前3週高値超',csv.DictReader(f).fieldnames)
        saved=json.loads((new/'run.json').read_text(encoding='utf-8'))
        self.assertFalse(saved['settings']['weekly']['require_breakout'])

    def test_weekly_cli_does_not_require_api_key(self):
        _,manifest=run_demo(self.root)
        with patch.dict(os.environ,{},clear=True),patch('sys.stdout',new_callable=io.StringIO):
            code=main(['weekly','--run-id',manifest['run_id'],'--root',str(self.root)])
        self.assertEqual(code,0)
        results=list((self.root/'outputs').glob('*_stage2'))
        self.assertEqual(len(results),1)
        self.assertTrue((results[0]/'weekly_audit.csv').exists())


if __name__=='__main__': unittest.main()
