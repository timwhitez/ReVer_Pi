from pathlib import Path
import os, sys, subprocess,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from reverpi_study.analysis import analyze
from reverpi_study.safeio import load

ROOT=Path(__file__).resolve().parents[3]
EVIDENCE=Path(os.environ.get('REVERPI_S3_EVIDENCE',str(ROOT/'evidence'/'RC7_1_Paid_Evidence')))

@pytest.fixture(scope='module')
def result():
    # Missing raw evidence is an error, not a silently skipped success.
    assert EVIDENCE.is_dir(), 'Provide the distributed raw evidence or REVERPI_S3_EVIDENCE'
    return analyze(EVIDENCE)

def test_known_raw_totals(result):
    assert result['actual_real_requests_reanalyzed']==15
    assert result['observed_tokens_reanalyzed']==251761
    assert result['completed_pairs']==1 and result['source_clusters_with_completed_pair']==1
    assert result['usd'] is None and result['new_model_calls']==0

def test_completed_frontier(result):
    r=result['rounds'][1]
    assert r['logical_full']['total_tokens']==113462
    assert r['logical_projected']['total_tokens']==86162
    assert set(r['nondominated_arms'])=={'full','projected'}
    assert r['phases']['full']['budget_curve']['curve'][2]['correct_by_k']==1
    assert r['phases']['projected']['budget_curve']['curve'][2]['correct_by_k']==0
    assert r['phases']['projected']['budget_curve']['curve'][3]['correct_by_k']==1

def test_first_stop_not_complete(result):
    r=result['rounds'][0]
    assert r['completed_pair'] is False
    assert r['phases']['projected']['status']=='unvisited'
    assert r['phases']['full']['budget_curve']['curve'][2]['correct_by_k'] is None

def test_cli_roundtrip(tmp_path):
    cli=Path(__file__).resolve().parents[1]/'run.py';out=tmp_path/'analysis.json'
    env={k:v for k,v in os.environ.items() if not any(s in k for s in ('TOKEN','SECRET','API_KEY','PASSWORD'))}
    env['PYTHONDONTWRITEBYTECODE']='1'
    p=subprocess.run([sys.executable,str(cli),'analyze','--evidence',str(EVIDENCE),'--out',str(out)],env=env,capture_output=True,text=True,timeout=20)
    assert p.returncode==0,p.stderr
    assert load(out)['observed_tokens_reanalyzed']==251761
    p=subprocess.run([sys.executable,str(cli),'analyze','--evidence',str(EVIDENCE),'--out',str(out)],env=env,capture_output=True,text=True,timeout=20)
    assert p.returncode==2 and 'Output exists' in p.stderr

def test_cli_no_live_subcommand():
    cli=Path(__file__).resolve().parents[1]/'run.py'
    p=subprocess.run([sys.executable,str(cli),'live'],capture_output=True,text=True,timeout=10)
    assert p.returncode==2
