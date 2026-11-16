"""Portfolio regressions: repeated executions, fees, partial exits and splits."""
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import unittest

try:
    from jp_screening.analysis.portfolio import HEADERS, prepare_snapshot, calculate_position
except ModuleNotFoundError:
    raise unittest.SkipTest('Install .[analysis].')
from jp_screening.core import JST, ScreeningError, TradingCalendar

DAY = date(2026, 1, 5)
DAYS = [DAY + timedelta(days=i) for i in range(5)]


def snapshot(rows=None):
    return dict(source=dict(spreadsheet_id='synthetic_sheet_1234567890', sheet_id=0, sheet_name='売買 履歴'),
        complete=True, observed_at='2026-01-09T18:00:00+09:00',
        values=[list(HEADERS), *([['2026/1/5', '2026/1/7', '1000', '架空企業', '特定', '現物', '買付', 100, 100, 10, 1, 0]]
                                if rows is None else rows)])


def trade(day=DAY, quantity=100, price=100, side='買付', costs=0, ordinal=2):
    return dict(trade_date=day, code='10000', name='架空企業', account='特定', side=side,
        quantity=D(quantity), unit_price=D(price), costs=D(costs), ordinal=ordinal)


def quotes():
    return {day:dict(close=D(110), factor=D(1)) for day in DAYS}


class PortfolioTests(unittest.TestCase):
    def test_end_of_day_valuation_waits_for_the_scheduled_publication_window(self):
        from jp_screening.analysis.portfolio import valuation_cutoff
        cal = TradingCalendar({DAY+timedelta(days=i):'1' if (DAY+timedelta(days=i)).weekday() < 5 else '0'
                               for i in range(-35, 5)})
        now = datetime(2026,1,7,16,tzinfo=JST)
        self.assertEqual(valuation_cutoff(cal, now), DAYS[1])
        self.assertEqual(valuation_cutoff(cal, now.replace(hour=18)), DAYS[2])

    def test_snapshot_repeat_rename_and_identical_execution(self):
        payload = snapshot()
        source, first, _, rows = prepare_snapshot(payload)
        again = deepcopy(payload)
        again['observed_at'] = '2026-01-09T19:00:00+09:00'
        again['source']['sheet_name'] = '変更後'
        self.assertEqual(prepare_snapshot(again)[:2], (source, first))
        again['values'].append(again['values'][1])
        _, second, _, repeated = prepare_snapshot(again)
        self.assertNotEqual(first, second)
        self.assertEqual(len(repeated), 2)
        self.assertNotEqual(repeated[0]['trade_key'], repeated[1]['trade_key'])

    def test_dates_from_sheets_serial_and_broker_text(self):
        payload = snapshot()
        payload['values'][1][0] = (DAY-date(1899,12,30)).days
        _, _, _, rows = prepare_snapshot(payload)
        self.assertEqual(rows[0]['trade_date'], DAY)
        self.assertEqual(rows[0]['costs'], D(11))

    def test_partial_snapshots_and_unsupported_or_invalid_trades_rejected(self):
        for field, value in [(7, 0), (8, 'NaN'), (7, 'Infinity'), (7, -1), (7, '1.2'), (5, '信用'), (1, '2025/12/30')]:
            payload = snapshot()
            payload['values'][1][field] = value
            with self.assertRaises(ScreeningError):
                prepare_snapshot(payload)
        payload = snapshot()
        payload['complete'] = False
        with self.assertRaises(ScreeningError):
            prepare_snapshot(payload)

    def test_buy_add_partial_sale_and_costs(self):
        trades = [trade(costs=10), trade(DAYS[1], price=120, ordinal=3),
                  trade(DAYS[2], quantity=50, price=130, side='売付', costs=5, ordinal=4)]
        value = calculate_position(trades, quotes(), set(DAYS), DAYS[-1])
        self.assertEqual(value['quantity'], 150)
        self.assertEqual(value['average_cost'], D('110.05'))
        self.assertEqual(value['realized'], D('992.50'))
        self.assertEqual(value['unrealized'], D('-7.50'))

    def test_split_then_buy_and_partial_sale(self):
        data = quotes()
        data[DAYS[1]] = dict(close=D(50), factor=D('0.5'))
        data[DAYS[2]]['close'] = D(60)
        trades = [trade(), trade(DAYS[1], quantity=100, price=50, ordinal=3),
                  trade(DAYS[2], quantity=100, price=60, side='売付', ordinal=4)]
        value = calculate_position(trades, data, set(DAYS), DAYS[2])
        self.assertEqual(value['quantity'], 200)
        self.assertEqual(value['average_cost'], 50)
        self.assertEqual(value['realized'], 1000)
        self.assertEqual(value['unrealized'], 2000)
        data[DAYS[3]] = dict(close=D(120), factor=D(2))
        value = calculate_position(trades, data, set(DAYS), DAYS[3])
        self.assertEqual(value['quantity'], 100)
        self.assertEqual(value['unrealized'], 2000)

    def test_buy_on_split_day_is_not_adjusted_twice(self):
        data = {DAY:dict(close=D(50), factor=D('0.5'))}
        value = calculate_position([trade(quantity=100, price=50)], data, {DAY}, DAY)
        self.assertEqual(value['quantity'], 100)
        self.assertEqual(value['unrealized'], 0)

    def test_missing_factor_or_price_and_oversell_never_show_zero_profit(self):
        data = quotes()
        del data[DAYS[1]]
        value = calculate_position([trade()], data, set(DAYS), DAYS[-1])
        self.assertEqual(value['status'], '株価・調整係数の欠損')
        self.assertIsNone(value['unrealized'])
        self.assertIsNone(value['quantity'])
        self.assertIsNone(value['average_cost'])
        value = calculate_position([trade(), trade(DAYS[1], quantity=200, side='売付')], quotes(), set(DAYS), DAYS[-1])
        self.assertEqual(value['status'], '売却数量と保有履歴の照合待ち')
        self.assertIsNone(value['realized'])

    def test_same_day_buys_wait_for_close_and_old_holding_uses_closed_day(self):
        value = calculate_position([trade(day=DAYS[1])], quotes(), set(DAYS), DAYS[1], DAY)
        self.assertEqual(value['status'], '買付日以降の終値待ち')
        self.assertIsNone(value['unrealized'])
        self.assertEqual(value['quantity'], 100)
        value = calculate_position([trade()], quotes(), set(DAYS), DAYS[1], DAY)
        self.assertEqual(value['status'], '評価済み')
        self.assertEqual(value['price_day'], DAY)
        self.assertEqual(value['unrealized'], 1000)

    def test_fully_sold_position_has_zero_value_and_actual_realized_profit(self):
        value = calculate_position([trade(costs=10), trade(DAYS[1], side='売付', price=120, costs=5)], quotes(), set(DAYS), DAYS[-1])
        self.assertEqual(value['quantity'], 0)
        self.assertEqual(value['market_value'], 0)
        self.assertEqual(value['realized'], 1985)


if __name__ == '__main__':
    unittest.main()
