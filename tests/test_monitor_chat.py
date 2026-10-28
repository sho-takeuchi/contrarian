"""Saved observations, follow-up scope and validated evidence for the read-only chat."""
from datetime import datetime, date, time
import unittest

try:
    from jp_screening.analysis.monitor_chat import select_records, record_answer
except ModuleNotFoundError:
    raise unittest.SkipTest('Install .[analysis].')
from jp_screening.core import JST, ScreeningError

START = date(2026, 1, 5)
END = date(2026, 1, 7)


def period(code='10000', direction='up', ended=True, uncertain=False, analysis_id='analysis'):
    measurement = dict(status='評価済み', baseline='100', end_close='110' if direction == 'up' else '90',
                       return_pct='10' if direction == 'up' else '-10')
    return dict(id=code, code=code, start_date=START, end_date=END if ended else None,
        state='ended' if ended else 'passing', direction=direction, digest='current', analysis_id=analysis_id,
        content=dict(name='合成企業' if code == '10000' else '別企業', last_date=str(END),
            continuity_unknown=uncertain, latest=measurement, exit=measurement if ended else None,
            start_run='start', end_run='exit' if ended else None,
            failure=dict(stage=1, reason='許容上限超過', failed_conditions=['許容上限超過'],
                         values={'current_close':'110','upper_limit':'105'}) if ended else None))


def source(**changes):
    return dict(id='source', code='10000', provider='edinet', source_key='doc',
        title='事業提携', url='https://example.org/doc', body='合成企業は新製品の販売拡大に向けた事業提携を発表しました。',
        published_at=datetime.combine(START, time(12), JST), fetched_at=datetime.combine(END, time(18), JST),
        content={'matched':True,'body_state':'本文取得済み'}, **changes)


def analysis(**changes):
    row=source()
    content=dict(period_digest='current', conclusion='関連した可能性のある資料を確認しました。', coverage=[],
        factors=[dict(source_id=row['id'],quote=row['body'],category='事業・提携')])
    content.update(changes)
    return dict(id='analysis', period_id='10000', content=content, created_at=datetime.combine(END,time(18),JST))


class MonitorChatTests(unittest.TestCase):
    def test_code_name_and_direction_are_conjunctive(self):
        records=[period(),period('20000','down')]
        self.assertEqual([r['code'] for r in select_records('1000の理由は？', records)[0]], ['10000'])
        self.assertEqual([r['code'] for r in select_records('合成企業はなぜ終了した？', records)[0]], ['10000'])
        self.assertFalse(select_records('1000は下落して終了した？', records)[0])
        self.assertFalse(select_records('9999の理由は？', records)[0])
        self.assertFalse(select_records('未登録の会社の通過終了理由は？', records)[2])
        self.assertFalse(select_records('この銘柄はなぜ上昇して通過終了した？', records)[2])
        self.assertEqual(len(select_records('1000 2000の理由は？',records)[0]),2)

    def test_date_is_not_mistaken_for_a_stock_code(self):
        records=[period()]
        chosen,scope,_=select_records('2026-01-06から2026-01-08の上昇終了を教えて', records)
        self.assertEqual(scope['codes'], [])
        self.assertEqual(len(chosen), 1)
        self.assertFalse(select_records('2026-01-08の上昇終了を教えて', records)[0])
        for question in ['2026-02-30の記録','2026-01-08から2026-01-06の記録']:
            with self.assertRaises(ScreeningError): select_records(question, records)

    def test_followup_retains_company_and_can_reset_to_all(self):
        records=[period(),period('20000','down')]
        _,scope,_=select_records('1000の通過終了は？', records)
        followup,new_scope,_=select_records('その根拠を詳しく教えて', records, scope)
        self.assertEqual([r['code'] for r in followup], ['10000'])
        chosen,_,_=select_records('では全体の下落は？', records, new_scope)
        self.assertEqual([r['code'] for r in chosen], ['20000'])

    def test_continuity_unknown_is_not_an_up_exit(self):
        records=[period(uncertain=True),period('20000',ended=False)]
        self.assertFalse(select_records('上昇して通過終了した理由は？',records)[0])
        chosen,_,_=select_records('要確認の銘柄は？',records)
        self.assertEqual(len(chosen), 1)

    def test_pagination_does_not_repeat_first_ten(self):
        records=[period(str(10000+i)) for i in range(15)]
        chosen,scope,_=select_records('上昇して通過終了した理由は？',records)
        next_chosen,next_scope,_=select_records('残りを教えて',records,scope)
        self.assertEqual(chosen,next_chosen)
        self.assertEqual(next_scope['offset'],10)
        self.assertEqual(len(next_chosen[next_scope['offset']:next_scope['offset']+10]),5)

    def test_saved_numbers_and_reasons_are_separate_from_price_factors(self):
        text,ids=record_answer(period(),analysis(),[source()])
        self.assertIn('+10.00％',text)
        self.assertIn('条件を外れた理由',text)
        self.assertIn('許容上限超過',text)
        self.assertIn('値動きに関連する資料',text)
        self.assertIn('https://example.org/doc',text)
        self.assertIn(source()['body'],text)
        self.assertEqual(ids,['source'])

    def test_stale_analysis_unknown_source_and_after_close_are_not_cited(self):
        stale=analysis(period_digest='old')
        text,ids=record_answer(period(),stale,[source()])
        self.assertIn('保存待ち',text)
        self.assertNotIn('https://example.org/doc',text)
        self.assertFalse(ids)
        for sources in [[],[{**source(),'body':'別の本文'}],
                [{**source(),'published_at':datetime.combine(END,time(16),JST)}],
                [source(),{**source(),'id':'withdrawn','body':'','fetched_at':datetime.combine(END,time(19),JST),
                           'content':{'body_state':'取下げ・不開示'}}]]:
            text,ids=record_answer(period(),analysis(),sources)
            self.assertFalse(ids)
            self.assertNotIn('https://example.org/doc',text)

    def test_ongoing_and_missing_prices_never_invent_a_return_or_exit(self):
        row=period(ended=False)
        text,_=record_answer(row,None,[])
        self.assertIn('終了はまだ確認されていません',text)
        self.assertNotIn('条件を外れた理由',text)
        row=period()
        row['content']['exit'].update(baseline=None,end_close=None,return_pct=None,status='必要価格の欠損')
        text,_=record_answer(row,None,[])
        self.assertIn('必要価格の欠損',text)
        self.assertNotIn('+0.00',text)


if __name__=='__main__': unittest.main()
