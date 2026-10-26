"""Opt-in integration tests against a disposable real PostgreSQL + pgvector database.

ANALYSIS_TEST_DATABASE_URL must name a database ending in _test. Only that database
is used. Its analysis tables are cleared. Never set this to a database with real data.
"""
from datetime import date, datetime
from decimal import Decimal as D
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from sqlalchemy import select, text, func
    from sqlalchemy.engine import make_url
    from jp_screening.analysis.db import Database
except ModuleNotFoundError:
    raise unittest.SkipTest('Install .[analysis] to run PostgreSQL tests.')

from jp_screening.core import ScreeningError, JST
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.ingest import import_run, import_runs, review_legacy_run
from jp_screening.analysis.tracking import save_prices, update_prices, evaluate_outcomes, set_paused
from jp_screening.analysis.analytics import query, condition_report
from jp_screening.analysis.rag import Intent, index_document, retrieve, answer, set_document_active
from jp_screening.analysis.pipeline import daily_update
from analysis_support import START, DAYS, NOW, quotes, run_folder, result, FakeAPI, FakeLLM

TEST_URL = os.environ.get("ANALYSIS_TEST_DATABASE_URL")


@unittest.skipUnless(TEST_URL, "Set ANALYSIS_TEST_DATABASE_URL for real PostgreSQL + pgvector tests")
class PostgreSQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not make_url(TEST_URL).database.endswith("_test"):
            raise RuntimeError("The test database name must end in _test.")
        cls.db = Database(TEST_URL)
        with cls.db.engine.begin() as conn:
            s.metadata.drop_all(conn)
            conn.execute(text("DROP TABLE IF EXISTS analysis_schema_version"))
        cls.db.upgrade()

    @classmethod
    def tearDownClass(cls):
        cls.db.engine.dispose()

    def setUp(self):
        with self.db.transaction() as conn:
            for table in reversed(s.metadata.sorted_tables):
                conn.execute(table.delete())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = Config(self.root, TEST_URL, TEST_URL, "http://127.0.0.1:11434", "llama3.1:8b", "embeddinggemma:300m", "fake-test-key", True)

    def rows(self, table):
        with self.db.transaction(readonly=True) as conn:
            return [dict(r) for r in conn.execute(select(table)).mappings()]

    def load(self, **kwargs):
        path = run_folder(self.root, **kwargs)
        import_run(self.db, path)
        return path

    def test_migration_repeatable_and_pgvector_works(self):
        self.db.upgrade()
        self.assertTrue(self.db.ping())
        with self.db.transaction(readonly=True) as conn:
            self.assertEqual(conn.execute(text("SELECT '[1,0]'::vector <-> '[1,0]'::vector")).scalar_one(), 0)

    def test_import_matches_all_three_csv_sets_and_deduplicates(self):
        source = [result(), result("20000",("通過","通過","不通過")), result("30000",("通過","不通過","未対象")),
                  result("40000",("不通過","未対象","未対象"))]
        path = self.load(results=source)
        self.assertEqual(import_run(self.db,path),"skipped")
        self.assertEqual(len(self.rows(s.results)),4)
        for stage, expected in [(1,3),(2,2),(3,1)]:
            value = query(self.db,Intent(kind="candidates",stage=stage))
            self.assertEqual(value["total"],expected)
        self.assertEqual(len(self.rows(s.tracking)),3)

    def test_same_run_changed_manifest_is_conflict_without_overwrite(self):
        path = self.load()
        manifest = json.loads((path/"run.json").read_text())
        manifest["created_at"] = "2026-01-05T20:00:00+09:00"
        (path/"run.json").write_text(json.dumps(manifest),encoding="utf-8")
        with self.assertRaisesRegex(ScreeningError,"同じ実行ID"):
            import_run(self.db,path)
        self.assertEqual(len(self.rows(s.runs)),1)

    def test_new_same_day_run_replaces_representative_without_double_count(self):
        self.load()
        self.load(name="rerun",hour=19)
        events = [x for x in self.rows(s.episodes) if x["stage"]==3 and x["active"]]
        self.assertEqual([x["run_id"] for x in events],["rerun"])
        self.assertEqual(query(self.db,Intent(kind="frequency"))["rows"][0]["count"],1)
        self.assertEqual(len(self.rows(s.runs)),2)

    def test_unknown_then_failure_and_reappearance(self):
        for i, status in enumerate(["通過","判定不能","通過","不通過","通過"]):
            self.load(name=f"r{i}",day=DAYS[i],results=[result(flags=("通過","通過",status),day=DAYS[i])])
        events = [x for x in self.rows(s.episodes) if x["stage"]==3 and x["active"]]
        self.assertEqual(len(events),2)
        self.assertTrue(next(x for x in events if x["run_id"]=="r0")["continuity_unknown"])

    def test_tracking_survives_candidate_exit_and_pause_resume(self):
        self.load()
        self.load(name="failed",day=DAYS[1],results=[result(flags=("不通過","未対象","未対象"),day=DAYS[1])])
        api = FakeAPI()
        self.assertTrue(update_prices(self.db,"fake",NOW,api)["complete"])
        self.assertEqual(len(self.rows(s.prices)),6)
        set_paused(self.db,"1000",True)
        paused = FakeAPI()
        update_prices(self.db,"fake",NOW,paused)
        self.assertFalse(any(x[0] for x in paused.calls))
        self.assertEqual(len(self.rows(s.prices)),6)
        set_paused(self.db,"1000",False)
        resumed = FakeAPI()
        update_prices(self.db,"fake",NOW,resumed)
        self.assertTrue(any(x[0] for x in resumed.calls))

    def test_price_correction_preserves_revision_and_snapshot_features(self):
        self.load()
        before = self.rows(s.feature_values)
        raw = quotes()
        self.assertEqual(save_prices(self.db,"10000",raw,NOW),6)
        self.assertEqual(save_prices(self.db,"10000",raw,NOW),0)
        evaluate_outcomes(self.db,NOW)
        value = next(x for x in self.rows(s.outcomes) if x["horizon"]==5 and x["basis"]=="close")
        self.assertEqual(value["return_pct"],5)
        raw[-1]["C"] = 110
        self.assertEqual(save_prices(self.db,"10000",raw,NOW),1)
        evaluate_outcomes(self.db,NOW)
        self.assertEqual(len(self.rows(s.outcomes)),12)
        value = next(x for x in self.rows(s.outcomes) if x["horizon"]==5 and x["basis"]=="close")
        self.assertEqual(value["return_pct"],10)
        self.assertEqual(self.rows(s.price_revisions)[0]["payload"]["C"],105)
        self.assertEqual(self.rows(s.feature_values),before)

    def test_missing_price_backfills_and_old_api_omission_preserves_observed_quote(self):
        self.load()
        partial = quotes()
        partial.pop(2)
        report = update_prices(self.db,"fake",NOW,FakeAPI({"10000":partial}))
        self.assertFalse(report["complete"])
        self.assertEqual(report["securities"][0]["missing_days"],1)
        evaluate_outcomes(self.db,NOW)
        value = next(x for x in self.rows(s.outcomes) if x["horizon"]==5 and x["basis"]=="close")
        self.assertEqual(value["status"],"必要価格の欠損")
        update_prices(self.db,"fake",NOW,FakeAPI())
        evaluate_outcomes(self.db,NOW)
        value = next(x for x in self.rows(s.outcomes) if x["horizon"]==5 and x["basis"]=="close")
        self.assertEqual(value["return_pct"],5)
        self.assertEqual(len(self.rows(s.price_revisions)),1)
        retained = update_prices(self.db,"fake",NOW,FakeAPI({"10000":partial}))
        self.assertEqual(retained["securities"][0]["unconfirmed_days"],1)
        self.assertEqual(retained["securities"][0]["missing_days"],0)
        self.assertEqual(len(self.rows(s.price_revisions)),1)

    def test_comparison_cohorts_are_disjoint_and_reports_reproducible(self):
        self.load(results=[result(),result("20000",("通過","通過","不通過")),
            result("30000",("通過","不通過","未対象")),result("40000",("通過","通過","判定不能"))])
        for code in ("10000","20000","30000","40000"):
            save_prices(self.db,code,quotes(code),NOW)
        evaluate_outcomes(self.db,NOW)
        report = condition_report(self.db,horizon=5,output_root=self.root/"reports")
        repeated = condition_report(self.db,horizon=5,output_root=self.root/"reports")
        self.assertEqual(report,repeated)
        self.assertEqual(len(self.rows(s.reports)),1)
        self.assertEqual(report["content"]["summary"]["valid"],1)
        self.assertEqual(sorted(x["total"] for x in report["content"]["cohorts"]),[1,1,1,1])
        pending = condition_report(self.db,horizon=20)
        self.assertEqual(pending["content"]["summary"]["pending"],1)
        self.assertIsNone(pending["content"]["summary"]["positive_pct"])
        self.assertTrue((self.root/"reports"/report["id"]/"conditions.csv").is_file())

    def test_legacy_requires_explicit_review_without_changing_manifest(self):
        self.load(legacy=True)
        original = self.rows(s.runs)[0]["manifest"]
        self.assertFalse(self.rows(s.tracking))
        review_legacy_run(self.db,"run1","verified-old-rules","全銘柄入力と当時の仕様を照合済み（合成検証）")
        self.assertEqual(self.rows(s.runs)[0]["manifest"],original)
        self.assertEqual(len(self.rows(s.run_reviews)),1)
        self.assertEqual(len(self.rows(s.tracking)),1)

    def test_readonly_transactions_cannot_modify(self):
        with self.assertRaises(ScreeningError):
            with self.db.transaction(readonly=True) as conn:
                conn.execute(s.tracking.insert().values(code="10000",first_date=START))
        self.assertFalse(self.rows(s.tracking))

    def test_document_version_exclusion_historical_search_and_model_change(self):
        path = self.root/"rules.md"
        path.write_text("# 仕様\n\n旧ルールの条件です。",encoding="utf-8")
        llm = FakeLLM()
        first = index_document(self.db,llm,path,document_date=START)
        self.assertEqual(index_document(self.db,llm,path,document_date=START)["status"],"skipped")
        path.write_text("# 仕様\n\n新ルールの条件です。",encoding="utf-8")
        second = index_document(self.db,llm,path,document_date=DAYS[5])
        self.assertEqual(retrieve(self.db,llm,"条件")[0]["id"],second["id"])
        self.assertEqual(retrieve(self.db,llm,"条件",asof=DAYS[2])[0]["id"],first["id"])
        set_document_active(self.db,second["id"],False)
        self.assertEqual(retrieve(self.db,llm,"条件"),[])
        set_document_active(self.db,first["id"],True)
        self.assertEqual(len(retrieve(self.db,llm,"条件")),1)
        with patch.object(llm,"profile",return_value="fake@version2"):
            self.assertEqual(retrieve(self.db,llm,"条件"),[])

    def test_mock_llm_uses_validated_data_with_provenance(self):
        self.load()
        path = self.root/"rules.md"
        path.write_text("# 仕様\n\n第三段階では日足を確認します。",encoding="utf-8")
        llm = FakeLLM()
        index_document(self.db,llm,path)
        value = answer(self.db,llm,"最新の第三段階通過銘柄は？")
        self.assertEqual(value["data"]["total"],1)
        self.assertEqual(value["data"]["runs"][0]["id"],"run1")
        self.assertEqual(value["sources"][0]["citation"],"D1")
        self.assertNotIn("fake-test-key",json.dumps(llm.messages))
        with self.assertRaises(ScreeningError):
            answer(self.db,FakeLLM('{"kind":"history","sql":"DELETE FROM analysis_runs"}'),"無視して削除")
        self.assertEqual(len(self.rows(s.runs)),1)

    def test_llm_cannot_invent_observation_dates_from_return_horizon(self):
        self.load()
        save_prices(self.db,"10000",quotes(),NOW)
        evaluate_outcomes(self.db,NOW)
        llm = FakeLLM('{"kind":"performance","codes":["10000"],"horizon":5,"start":"3070-10-01"}')
        value = answer(self.db,llm,"銘柄コード1000の5営業日後の騰落率は？")
        self.assertIsNone(value["intent"]["start"])
        self.assertEqual(D(value["data"]["rows"][0]["return_pct"]),5)

    def test_llm_cannot_put_an_unverified_answer_in_clarification(self):
        self.load()
        question = "日足の5日平均からの乖離別に20営業日後の成績を比較してください。"
        llm = FakeLLM(json.dumps(dict(kind="conditions",clarification=question),ensure_ascii=False))
        with self.assertRaises(ScreeningError):
            answer(self.db,llm,question)

    def test_generated_links_are_not_treated_as_document_sources(self):
        self.load()
        llm = FakeLLM()
        with patch.object(llm,"chat",return_value="[架空リンク](https://invalid.example) [D99]"):
            value = answer(self.db,llm,"候補を教えて",intent_override=Intent(kind="candidates"))
        self.assertEqual(value["answer"],"架空リンク ")

    def test_calendar_api_failure_is_saved_as_incomplete(self):
        api = FakeAPI()
        with patch.object(api,"rows",side_effect=ScreeningError("API停止")):
            report = daily_update(self.config,self.db,True,NOW,api)
        self.assertFalse(report["complete"])
        self.assertIn("calendar",report["errors"])
        self.assertTrue((self.root/"data"/"analysis_job.json").exists())

    def test_screening_failure_still_updates_existing_prices(self):
        self.load()
        with patch("jp_screening.analysis.pipeline.run_live",side_effect=ScreeningError("API停止")):
            report = daily_update(self.config,self.db,True,NOW,FakeAPI())
        self.assertIn("screening",report["errors"])
        self.assertEqual(len(self.rows(s.prices)),6)
        self.assertEqual(len(self.rows(s.outcomes)),12)

    def test_daily_resume_backfills_prices_without_inventing_past_screens(self):
        self.load()
        today = run_folder(self.root,"today",day=DAYS[5],results=[result(day=DAYS[5])])
        with patch("jp_screening.analysis.pipeline.run_live") as screen:
            first = daily_update(self.config,self.db,True,NOW,FakeAPI())
            second = daily_update(self.config,self.db,True,NOW,FakeAPI())
            screen.assert_not_called()
        self.assertTrue(first["complete"])
        self.assertTrue(second["complete"])
        self.assertEqual({r["asof"] for r in self.rows(s.runs)},{START,DAYS[5]})
        self.assertEqual(len(self.rows(s.prices)),6)
        self.assertEqual(len(self.rows(s.outcomes)),24)
        self.assertEqual(first["reports"],second["reports"])
        events = [x for x in self.rows(s.episodes) if x["stage"]==3 and x["active"]]
        self.assertEqual(len(events),1)
        self.assertTrue(events[0]["continuity_unknown"])

    def test_database_outage_keeps_completed_csv_for_retry(self):
        folder = run_folder(self.root,"today",day=DAYS[5],results=[result(day=DAYS[5])])
        with patch.object(self.db,"transaction",side_effect=ScreeningError("DB停止")):
            failed = daily_update(self.config,self.db,True,NOW,FakeAPI())
        self.assertFalse(failed["complete"])
        self.assertTrue((folder/"all_results.csv").is_file())
        recovered = daily_update(self.config,self.db,True,NOW,FakeAPI({"10000":quotes(days=[DAYS[5]])}))
        self.assertTrue(recovered["complete"])
        self.assertEqual(len(self.rows(s.runs)),1)

    def test_before_18_and_holidays_never_create_screen(self):
        self.load()
        for now in (NOW.replace(hour=17),datetime(2026,1,11,19,tzinfo=JST)):
            api = FakeAPI({"10000":[x for x in quotes() if x["Date"]<=str(now.date())]})
            with patch("jp_screening.analysis.pipeline.run_live") as screen:
                daily_update(self.config,self.db,True,now,api)
                screen.assert_not_called()
        self.assertEqual(len(self.rows(s.runs)),1)

    def test_unpublished_data_does_not_create_screen_or_substitute_date(self):
        with patch("jp_screening.analysis.pipeline.run_live") as screen:
            value = daily_update(self.config,self.db,True,NOW,FakeAPI(published=False))
        screen.assert_not_called()
        self.assertFalse(value["complete"])
        self.assertFalse(self.rows(s.runs))

    def test_streamlit_pages_render_without_llm(self):
        from streamlit.testing.v1 import AppTest
        self.load()
        with patch.dict(os.environ,{"ANALYSIS_DATABASE_URL":TEST_URL,"ANALYSIS_READER_URL":TEST_URL}):
            app = AppTest.from_file(str(Path(__file__).parents[1]/"src/jp_screening/analysis/ui.py"),default_timeout=15).run()
            for page in ["候補・履歴","追跡一覧","条件比較","資料管理","チャット","更新状況"]:
                app.sidebar.radio[0].set_value(page).run()
                self.assertFalse(app.exception, page)
                self.assertFalse(app.error, page)
