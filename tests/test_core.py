from datetime import date, datetime, timedelta
from dataclasses import replace
from decimal import Decimal as D
import unittest

from jp_screening.core import (JST, Bar, ScreeningError, TradingCalendar, adjusted_bars,
    api_bars, code5, evaluate, monthly_bars, upper_limit, years_before)
from jp_screening.demo import fixture_data
from jp_screening.verification import compare_prices, reference_check


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cal, _, cls.asof, cls.start, cls.data = fixture_data()
        cls.meta, cls.listing, cls.rows = cls.data[0]
        cls.bars = adjusted_bars(cls.rows, cls.asof)

    def calc(self, bars=None, listing=None, asof=None):
        return evaluate(self.meta, listing or self.listing,
                        bars if bars is not None else self.bars, self.cal, asof or self.asof, self.start)

    def test_limits(self):
        for low, expected in ((100,150),(200,300),(300,400),(500,600),(1000,1200),(2000,2400)):
            with self.subTest(low=low):
                self.assertEqual(upper_limit(D(low)), D(expected))

    def test_limit_equality_and_excess(self):
        for current, expected in ((150,True),(D('150.1'),False),(99,True)):
            bars = [replace(b, close=D(current)) if b.day == self.asof else b for b in self.bars]
            result, _ = self.calc(bars)
            self.assertEqual(result['condition_a'], expected)

    def test_three_conditions_and_reference(self):
        result, months = self.calc()
        self.assertEqual(result['status'], '通過')
        self.assertEqual(result['required_months'],120)
        self.assertEqual(result['available_months'],120)
        self.assertEqual(reference_check(result, months, self.bars, self.listing, self.asof, self.cal), [])

    def test_relaxed_lows_and_breach(self):
        result, _ = self.calc()
        self.assertTrue(result['condition_b'])
        bars = [replace(b, low=D(99)) if b.day == date(2026,8,5) else b for b in self.bars]
        result, _ = self.calc(bars)
        self.assertFalse(result['condition_b'])

    def test_low_equal_allowed(self):
        bars = [replace(b, low=D(100)) if b.day == date(2026,8,5) else b for b in self.bars]
        self.assertTrue(self.calc(bars)[0]['condition_b'])

    def test_average_equal_fails(self):
        terminal = self.cal.month_end(date(2026,9,1))
        bars = [replace(b, close=D('117.5')) if b.day == terminal else b for b in self.bars]
        self.assertFalse(self.calc(bars)[0]['condition_c'])  # 2*117.5 == 115+120

    def test_missing_latest_disqualifies_but_evaluates_other_conditions(self):
        bars = [replace(b, close=None) if b.day == self.asof else b for b in self.bars]
        result, _ = self.calc(bars)
        self.assertEqual(result['status'],'判定不能')
        self.assertIsNone(result['condition_a'])
        self.assertTrue(result['condition_b'])

    def test_missing_terminal_not_forward_filled(self):
        end = self.cal.month_end(date(2025,3,1))
        bars = [replace(b, close=None) if b.day == end else b for b in self.bars]
        result, months = self.calc(bars)
        self.assertIsNone(months['2025-03'].close)
        self.assertEqual(result['available_months'],119)
        self.assertIsNone(result['condition_a'])

    def test_month_end_no_trade_uses_last_trade_in_same_month(self):
        end = self.cal.month_end(date(2025,3,1))
        previous = self.cal.sessions(date(2025,3,1),end)[-2]
        bars = [replace(b, close=None, open=None, high=None, low=None) if b.day == end else b for b in self.bars]
        result, months = self.calc(bars)
        month = months['2025-03']
        self.assertEqual(month.close, next(b.close for b in bars if b.day == previous))
        self.assertEqual(month.close_day, previous)
        self.assertEqual(month.close_lag_sessions, 1)
        self.assertEqual(month.close_source, 'last_trade_in_month')
        self.assertEqual(result['available_months'], 120)
        self.assertIsNotNone(result['condition_a'])

    def test_multiple_no_trade_days_at_month_end(self):
        sessions = self.cal.sessions(date(2025,3,1),self.cal.month_end(date(2025,3,1)))
        null_days = set(sessions[-3:])
        bars = [replace(b, close=None, open=None, high=None, low=None) if b.day in null_days else b for b in self.bars]
        _, months = self.calc(bars)
        self.assertEqual(months['2025-03'].close_day, sessions[-4])
        self.assertEqual(months['2025-03'].close_lag_sessions, 3)

    def test_entire_month_no_trade_never_uses_prior_month(self):
        bars = [replace(b, close=None, open=None, high=None, low=None) if b.day.strftime('%Y-%m') == '2025-03' else b for b in self.bars]
        result, months = self.calc(bars)
        self.assertIsNone(months['2025-03'].close)
        self.assertIsNone(months['2025-03'].close_day)
        self.assertIsNone(result['condition_a'])

    def test_missing_record_is_not_treated_as_no_trade(self):
        end = self.cal.month_end(date(2025,3,1))
        bars = [b for b in self.bars if b.day != end]
        _, months = self.calc(bars)
        self.assertIsNone(months['2025-03'].close)

    def test_no_trade_search_stops_at_record_gap(self):
        sessions = self.cal.sessions(date(2025,3,1),self.cal.month_end(date(2025,3,1)))
        bars = [replace(b, close=None, open=None, high=None, low=None) if b.day == sessions[-1] else b
                for b in self.bars if b.day != sessions[-2]]
        _, months = self.calc(bars)
        self.assertIsNone(months['2025-03'].close)

    def test_legacy_policy_preserves_strict_month_end(self):
        end = self.cal.month_end(date(2025,3,1))
        bars = [replace(b, close=None, open=None, high=None, low=None) if b.day == end else b for b in self.bars]
        result, months = evaluate(self.meta,self.listing,bars,self.cal,self.asof,self.start,close_policy='month_end')
        self.assertIsNone(months['2025-03'].close)
        self.assertIsNone(result['condition_a'])

    def test_partial_oldest_month_does_not_reach_before_coverage(self):
        cutoff = date(2016,10,28)
        trimmed = TradingCalendar({k:v for k,v in self.cal.days.items() if k >= cutoff})
        bars = [replace(b, close=None, open=None, high=None, low=None)
                if b.day.month == 10 and b.day.year == 2016 else b for b in self.bars if b.day >= cutoff]
        _, months = evaluate(self.meta,self.listing,bars,trimmed,self.asof,cutoff)
        self.assertIsNone(months['2016-10'].close)

    def test_regular_month_end_records_actual_date(self):
        _, months = self.calc()
        month=months['2025-03']
        self.assertEqual(month.close_day,month.end_day)
        self.assertEqual(month.close_lag_sessions,0)
        self.assertEqual(month.close_source,'month_end')

    def test_missing_record_invalidates_recent_low(self):
        result, _ = self.calc([b for b in self.bars if b.day != date(2026,8,5)])
        self.assertIsNone(result['condition_b'])

    def test_official_null_not_missing_record(self):
        bars = [replace(b, close=None, low=None, high=None) if b.day == date(2026,8,5) else b for b in self.bars]
        self.assertTrue(self.calc(bars)[0]['condition_b'])

    def test_partial_oldest_month_usable_close(self):
        bars = [b for b in self.bars if b.day >= date(2016,10,21)]
        trimmed = TradingCalendar({k:v for k,v in self.cal.days.items() if k >= date(2016,10,21)})
        result, months = evaluate(self.meta,self.listing,bars,trimmed,self.asof,date(2016,10,21))
        self.assertTrue(result['condition_a'])
        self.assertIsNone(months['2016-10'].low)
        self.assertIsNotNone(months['2016-10'].close)

    def test_missing_oldest_terminal_does_not_shorten(self):
        oldest = self.cal.month_end(date(2016,10,1))
        result, _ = self.calc([b for b in self.bars if b.day != oldest])
        self.assertEqual(result['required_months'],120)
        self.assertIsNone(result['condition_a'])

    def test_listing_boundaries(self):
        for listing, expected_status, months in ((date(2018,10,21),None,96),
            (date(2018,10,22),'対象外',None),(date(2016,10,21),None,120)):
            result, _ = self.calc(listing=listing)
            if expected_status:
                self.assertEqual(result['status'], expected_status)
            else:
                self.assertNotEqual(result['status'],'対象外')
                self.assertEqual(result['required_months'],months)

    def test_unknown_listing_not_inferred(self):
        result, _ = evaluate(self.meta,None,self.bars,self.cal,self.asof,self.start)
        self.assertEqual(result['status'],'判定不能')
        self.assertIn('上場日不明',result['reason'])

    def test_current_month_excluded_from_floor(self):
        bars = [replace(b, close=D(1)) if b.day == self.asof else b for b in self.bars]
        self.assertEqual(self.calc(bars)[0]['lowest_close'], D(100))

    def test_month_end_uses_current_for_average_not_floor(self):
        end = date(2026,10,30)
        bars = self.bars + [Bar(d, '10000', D(90), D(80), D(95))
            for d in self.cal.sessions(self.asof+timedelta(days=1),end)]
        result, _ = self.calc(bars, asof=end)
        self.assertEqual(result['m1_month'],'2026-10')
        self.assertEqual(result['comparison_end'],'2026-09')
        self.assertEqual(result['lowest_close'],D(100))
        self.assertFalse(result['condition_b'])

    def test_calendar_holiday_monthend_and_ose(self):
        days = dict(self.cal.days)
        days[date(2026,10,30)]='3'  # derivatives-only holiday trading
        cal = TradingCalendar(days)
        self.assertEqual(cal.month_end(date(2026,10,1)), date(2026,10,29))
        self.assertEqual(cal.last_complete_month(date(2026,10,29)),date(2026,10,1))

    def test_target_timing(self):
        with self.assertRaises(ScreeningError):
            self.cal.target(datetime(2026,10,21,15,29,tzinfo=JST))
        self.assertEqual(self.cal.target(datetime(2026,10,21,16,30,tzinfo=JST)),self.asof)
        self.assertEqual(self.cal.target(datetime(2026,10,25,10,tzinfo=JST)),date(2026,10,23))

    def test_calendar_gap_rejected(self):
        days=dict(self.cal.days)
        del days[date(2026,8,5)]
        with self.assertRaises(ScreeningError):
            evaluate(self.meta,self.listing,self.bars,TradingCalendar(days),self.asof,self.start)

    def test_split_and_reverse_split_same_day_shift(self):
        rows=[dict(Date='2026-10-19',Code='10000',O='980',H='1000',L='960',C='980',AdjFactor='1'),
              dict(Date='2026-10-20',Code='10000',O='480',H='490',L='470',C='480',AdjFactor='0.5'),
              dict(Date='2026-10-21',Code='10000',O='4800',H='4900',L='4700',C='4800',AdjFactor='10')]
        bars=adjusted_bars(rows,date(2026,10,21))
        self.assertEqual([b.close for b in bars],[D('4900.0'),D('4800.0'),D('4800.0')])
        for row,bar in zip(rows,bars):
            row.update(AdjC=str(bar.close),AdjL=str(bar.low),AdjH=str(bar.high),AdjO=str(bar.open))
        self.assertEqual(compare_prices(bars,api_bars(rows,date(2026,10,21))),[])

    def test_future_factor_excluded(self):
        rows=[dict(Date='2026-10-20',Code='10000',O='100',H='100',L='100',C='100',AdjFactor='1'),
              dict(Date='2026-10-21',Code='10000',O='50',H='50',L='50',C='50',AdjFactor='.5')]
        self.assertEqual(adjusted_bars(rows,date(2026,10,20))[0].close,D(100))

    def test_rounding_only_at_provider_input_precision(self):
        rows=[dict(Date='2026-10-20',Code='10000',O='1.05',H='1.05',L='1.05',C='1.05',AdjFactor='1')]
        self.assertEqual(adjusted_bars(rows,date(2026,10,20))[0].close,D('1.1'))

    def test_codes_and_leap_year(self):
        self.assertEqual(code5('001A'),'001A0')
        self.assertEqual(code5('25935'),'25935')
        self.assertEqual(years_before(date(2024,2,29),10),date(2014,2,28))

    def test_duplicate_and_missing_factor_rejected(self):
        with self.assertRaises(ScreeningError):
            adjusted_bars([self.rows[0],self.rows[0]],self.asof)
        with self.assertRaises(ScreeningError):
            adjusted_bars([{**self.rows[0],'AdjFactor':None}],self.asof)

    def test_zero_after_provider_rounding_rejected(self):
        row={**self.rows[0],'O':'.01','H':'.01','L':'.01','C':'.01'}
        with self.assertRaises(ScreeningError):
            adjusted_bars([row],self.asof)

    def test_mixed_codes_rejected(self):
        rows=[self.rows[0],{**self.rows[1],'Code':'99990'}]
        with self.assertRaises(ScreeningError):
            adjusted_bars(rows,self.asof)


if __name__ == '__main__':
    unittest.main()
