"""Explicit live-model check on synthetic data in a disposable test database."""
import json
from decimal import Decimal
import os
from pathlib import Path
import tempfile

from sqlalchemy.engine import make_url
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.db import Database
from jp_screening.analysis.ingest import import_run
from jp_screening.analysis.tracking import save_prices, evaluate_outcomes
from jp_screening.analysis.rag import Ollama, index_document, answer
from analysis_support import run_folder, quotes, NOW


class RecordingOllama(Ollama):
    def chat(self, messages, schema=None):
        text = super().chat(messages,schema)
        if schema:
            print(json.dumps(dict(model_intent=text),ensure_ascii=False),flush=True)
        return text


def main():
    url = os.environ["ANALYSIS_TEST_DATABASE_URL"]
    if not make_url(url).database.endswith("_test"):
        raise RuntimeError("Use a disposable database ending in _test.")
    db = Database(url)
    db.upgrade()
    with db.transaction() as conn:
        for table in reversed(s.metadata.sorted_tables):
            conn.execute(table.delete())
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = Config(root,url,url,"http://127.0.0.1:11434","llama3.1:8b","embeddinggemma:300m","",False)
        llm = RecordingOllama(config)
        import_run(db,run_folder(root))
        save_prices(db,"10000",quotes(),NOW)
        evaluate_outcomes(db,NOW)
        path = root/"rules.md"
        path.write_text("# 追跡の仕様\n\n第三段階を通過した銘柄は、候補から外れた後も追跡を続けます。\n\n"
                        "価格変化は配当と売買費用を含みません。数値はPythonで計算します。",encoding="utf-8")
        print(json.dumps(index_document(db,llm,path),ensure_ascii=False),flush=True)
        for question, kind in [
            ("最新の第3段階通過銘柄を教えてください。","candidates"),
            ("銘柄コード1000の通過開始から5営業日後の騰落率を、判定日終値基準で教えてください。","performance"),
            ("日足の5日平均からの乖離と20営業日後の騰落率の関係を条件別に比較してください。","conditions"),
            ("資料の仕様では、第三段階の候補から外れた後も追跡を続けますか？","documents")]:
            result = answer(db,llm,question)
            assert result["intent"]["kind"] == kind, result["intent"]
            assert result.get("answer"), result
            if kind=="performance":
                assert result["intent"]["horizon"]==5 and result["intent"]["basis"]=="close"
                assert Decimal(result["data"]["rows"][0]["return_pct"]) == 5, result["data"]
            print(json.dumps(dict(question=question,intent=result["intent"],answer=result["answer"],
                                  sources=len(result["sources"])),ensure_ascii=False),flush=True)


if __name__ == "__main__":
    main()
