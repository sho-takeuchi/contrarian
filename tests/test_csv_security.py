import csv
from datetime import datetime
from decimal import Decimal
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jp_screening.core import JST
from jp_screening.demo import fixture_data
from jp_screening.reporting import SafeCsvWriter, fingerprint, safe_cell, write_results
from jp_screening.runner import initialize, run_live

ROOT = Path(__file__).resolve().parents[1]


class CsvSecurityTests(unittest.TestCase):
    def test_formula_and_control_prefixes_are_neutralized(self):
        for text in ('=1+1','+1+1','-1+1','@SUM(1,1)','\t=1+1','\r=1+1','\n=1+1',
                     '  =1+1',' \t-1+1','\x00=1+1','\ufeff=1+1',
                     '＝1+1','＋1+1','－1+1','＠SUM(1,1)','\u3000＝1+1'):
            with self.subTest(text=repr(text)):
                self.assertEqual(safe_cell(text),"'"+text)
                self.assertEqual(safe_cell(safe_cell(text)),"'"+text)

    def test_numbers_dates_and_plain_text_are_preserved(self):
        for value in (Decimal('-12.5'),Decimal('100'),-20,0,1.5,True,False,None,
                      '001A0','通常銘柄名','2026-10-23','名前-通常',"'=1+1"):
            with self.subTest(value=value):
                self.assertEqual(safe_cell(value),value)
        output=io.StringIO(newline='')
        SafeCsvWriter(output).writerow([Decimal('-12.5'),-20,False,None,'-1+1'])
        self.assertEqual(next(csv.reader(io.StringIO(output.getvalue()))),
                         ['-12.5','-20','False','',"'-1+1"])

    def test_commas_quotes_and_embedded_newlines_remain_one_cell(self):
        payload='=1+1,";=2+2\n=3+3'
        output=io.StringIO(newline='')
        SafeCsvWriter(output).writerows([[payload,'normal']])
        rows=list(csv.reader(io.StringIO(output.getvalue(),newline='')))
        self.assertEqual(rows,[["'"+payload,'normal']])

    def test_results_protect_text_and_keep_numeric_signs_and_fingerprint(self):
        row=dict(code='10000',name='-1+1',status='通過',reason='',rise_pct=Decimal('-12.5'),
                 condition_a=True,condition_b=True,condition_c=True)
        digest=fingerprint([row])
        with tempfile.TemporaryDirectory(dir=ROOT/'data',prefix='csv_results_') as name:
            folder=Path(name)
            write_results(folder,[row],{})
            for filename in ('all_results.csv','candidates.csv'):
                with (folder/filename).open(encoding='utf-8-sig',newline='') as f:
                    saved=next(csv.DictReader(f))
                self.assertEqual(saved['銘柄名'],"'-1+1")
                self.assertEqual(saved['上昇率_pct'],'-12.5')
                self.assertEqual(saved['条件A_許容上限'],'通過')
        self.assertEqual(fingerprint([row]),digest)
        self.assertEqual(row['name'],'-1+1')

    def test_validation_code_export_uses_the_same_text_protection(self):
        cal,calendar,asof,start,data=fixture_data()
        meta,listing,rows=data[0]
        meta={**meta,'CoName':'=1+1'}
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None): return cls(2026,10,21,18,tzinfo=JST)
        class FakeClient:
            def __init__(self,*args): pass
        def sync(client,store,root,start,end):
            store.replace_source('csv_security_fixture',rows)
            return []
        with tempfile.TemporaryDirectory(dir=ROOT/'data',prefix='csv_validation_') as name:
            project=Path(name)
            initialize(project)
            source=project/'inputs'/'official.csv'
            source.write_text('Code,listing_date\n10000,2010-01-04\n',encoding='utf-8')
            with patch('jp_screening.runner.Client',FakeClient), \
                 patch('jp_screening.runner.datetime',Clock), \
                 patch('jp_screening.runner.context',return_value=(cal,calendar,asof,[],[meta])), \
                 patch('jp_screening.runner.sync_bulk',side_effect=sync), \
                 patch('jp_screening.runner.validate_sample',return_value=(
                     [{'price_match':True,'reference_match':True}],{})):
                folder,_=run_live(project,'all','AUDIT_DUMMY_NOT_REAL',listing_file=source)
            for filename in ('validation_codes.csv','all_results.csv','candidates.csv'):
                with (folder/filename).open(encoding='utf-8-sig',newline='') as f:
                    self.assertEqual(next(csv.DictReader(f))['銘柄名'],"'=1+1")


@unittest.skipUnless(shutil.which('git'),'Git is required for ignore-rule verification')
class InputIgnoreTests(unittest.TestCase):
    def test_all_input_formats_and_nested_files_are_ignored(self):
        ignored=['inputs/listing_dates.csv','inputs/listing_dates.source.json',
                 'inputs/listing_dates.csv.part','inputs/official.csv.gz',
                 'inputs/official.xlsx','inputs/nested/archive.zip',
                 '.venv/pyvenv.cfg','outputs/results.csv','.env']
        public=['README.md','pyproject.toml','requirements-lock.txt','src/jp_screening/reporting.py']
        with tempfile.TemporaryDirectory(dir=ROOT/'data',prefix='ignore_rules_') as name:
            folder=Path(name)
            subprocess.run(['git','init','-q','--template=',name],capture_output=True,check=True)
            (folder/'.gitignore').write_bytes((ROOT/'.gitignore').read_bytes())
            result=subprocess.run(['git','check-ignore','--no-index','-z','--stdin'],cwd=folder,
                input=('\0'.join(ignored+public)+'\0').encode(),capture_output=True,check=True)
            self.assertEqual(set(result.stdout.decode().strip('\0').split('\0')),set(ignored))


if __name__=='__main__':
    unittest.main()
