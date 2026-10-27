"""Optional live Ollama + pgvector acceptance check; synthetic test database only."""
from datetime import datetime,time
import os
from pathlib import Path
import tempfile

from sqlalchemy import select
from sqlalchemy.engine import make_url
from jp_screening.core import JST
from jp_screening.analysis import schema as s
from jp_screening.analysis.config import Config
from jp_screening.analysis.db import Database
from jp_screening.analysis.ingest import import_run
from jp_screening.analysis.monitoring import rebuild
from jp_screening.analysis.rag import Ollama
from jp_screening.analysis.research import index_pending,automatic_research
from jp_screening.analysis.sources import save_source
from jp_screening.analysis.tracking import save_prices
from analysis_support import START,DAYS,NOW,run_folder,result,quotes


def main():
    url=os.environ['ANALYSIS_TEST_DATABASE_URL']
    if not make_url(url).database.endswith('_test'): raise RuntimeError('Disposable database required')
    db=Database(url); db.upgrade()
    with db.transaction() as conn:
        for table in reversed(s.metadata.sorted_tables): conn.execute(table.delete())
    with tempfile.TemporaryDirectory() as tmp:
        root=Path(tmp)
        config=Config(root,url,url,'http://127.0.0.1:11434','llama3.1:8b','embeddinggemma:300m','',False,research_enabled=False)
        import_run(db,run_folder(root))
        failed=result(flags=('不通過','未対象','未対象'),day=DAYS[1])
        failed.update(stage1_reason='許容上限超過',reason='許容上限超過',condition_a=False)
        import_run(db,run_folder(root,'exit',day=DAYS[1],results=[failed]))
        prices=quotes(days=DAYS[:2]);prices[-1]['C']=110
        save_prices(db,'10000',prices,NOW)
        rebuild(db,datetime.combine(DAYS[1],time(19),JST))
        save_source(db,'10000',dict(provider='edinet',source_key='synthetic',title='合成企業の事業提携',
            url='https://example.org/synthetic',published_at=datetime.combine(START,time(12),JST),
            body='合成企業は、新製品の販売を拡大するため事業提携を発表しました。需要拡大への対応として販売網を広げます。',
            content={'matched':True,'body_state':'本文取得済み'}),NOW)
        llm=Ollama(config)
        assert index_pending(db,llm)==1
        report=automatic_research(db,config,NOW,ollama=llm)
        with db.transaction(readonly=True) as conn:
            saved=conn.execute(select(s.monitor_analyses).where(s.monitor_analyses.c.kind=='individual')).mappings().one()
            period=conn.execute(select(s.monitor_periods)).mappings().one()
        assert saved['state']=='complete',saved['content']
        assert period['direction']=='up'
        print({'individuals':report['individuals'],'state':saved['state'],'conclusion':saved['content']['conclusion'],
               'validated_factors':len(saved['content']['factors'])},flush=True)


if __name__=='__main__': main()
