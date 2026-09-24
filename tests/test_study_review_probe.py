import json
from pathlib import Path
import pytest
from reverpi.config import StudyConfig,Budget
from reverpi.data import make_fixtures,load_jsonl
from reverpi.study import run_study,grade
from reverpi.review import run_reviews,review_jobs,validate_findings
from reverpi.probe import probe
from reverpi.ledger import Ledger
from reverpi.errors import LabError
from reverpi.util import canonical


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_entire_mock_study_resume_no_new_calls(tmp_path,provider,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    make_fixtures(tmp_path/'data',3)
    cfg=StudyConfig(methods=['full','mask','summary','rever_lite'])
    file=tmp_path/'config.yaml';file.write_text(canonical(cfg.model_dump()))
    root=Path(__file__).resolve().parents[1]
    kwargs=dict(root=root,provider=p,config=cfg,public=tmp_path/'data/public.jsonl',gold_path=tmp_path/'data/gold.jsonl',out=tmp_path/'run',config_path=file)
    a=await run_study(**kwargs);before=Ledger(tmp_path/'run/ledger.sqlite',cfg.budget).totals()
    b=await run_study(**kwargs);after=Ledger(tmp_path/'run/ledger.sqlite',cfg.budget).totals()
    assert a['cells']==12 and b['finished']==12 and before==after
    rows=json.loads((tmp_path/'run/results.json').read_text())
    assert all(r['status']=='complete' for r in rows)
    assert json.loads((tmp_path/'run/manifest.json').read_text())['mock'] is True
    with pytest.raises(LabError,match='Frozen'):
        await run_study(**{**kwargs,'config':cfg.model_copy(update={'seed':2})})


@pytest.mark.asyncio
async def test_bad_gold_fails_before_spending(tmp_path,provider):
    make_fixtures(tmp_path/'data',1)
    gold=tmp_path/'data/gold.jsonl';rows=load_jsonl(gold);rows[0]['answers']['goal']=[];gold.write_text(canonical(rows[0]))
    cfg=StudyConfig();file=tmp_path/'config';file.write_text('{}')
    with pytest.raises(ValueError,match='nonempty list'):
        await run_study(tmp_path,provider,cfg,tmp_path/'data/public.jsonl',gold,tmp_path/'run',config_path=file)
    assert not (tmp_path/'run/ledger.sqlite').exists()


@pytest.mark.asyncio
async def test_heldout_locked_without_freeze(tmp_path,provider):
    make_fixtures(tmp_path/'data',1);p=tmp_path/'data/public.jsonl';rows=load_jsonl(p);rows[0]['split']='gate';p.write_text(canonical(rows[0]))
    with pytest.raises(LabError,match='freeze'):
        await run_study(tmp_path,provider,StudyConfig(split='gate'),p,tmp_path/'data/gold.jsonl',tmp_path/'run',config_path=tmp_path/'config')


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_protocol_probe_tool_reasoning_replay(tmp_path,provider,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    b=Budget(max_total_tokens=100000,per_cell_tokens=100000)
    report=await probe(p,b,tmp_path/'probe')
    assert report['compatible_for_this_probe'] and report['mock'] and not report['low_intensity_verified']
    assert report['checks']['function_call'] and report['checks']['tool_and_reasoning_replay']
    before=report['cost']['attempts']
    again=await probe(p,b,tmp_path/'probe')
    assert before==again['cost']['attempts']==3
    assert 'provider_sha' in report


@pytest.mark.asyncio
async def test_review_mock_never_certifies_code(tmp_path,provider):
    root=tmp_path/'code';(root/'src').mkdir(parents=True);(root/'src/ledger.py').write_text('def debit():\n    return 1\n')
    manifest,jobs=review_jobs(root,3)
    assert len(jobs)==3 and len({j['round'] for j in jobs})==3
    result=await run_reviews(root,provider,Budget(max_total_tokens=100000,per_cell_tokens=100000),tmp_path/'reviews',rounds=3)
    assert result['all_jobs_valid'] and result['independent_contexts']
    assert result['mock'] and not result['eligible_for_human_acceptance'] and not result['automatically_proves_correctness']


def test_review_evidence_must_be_from_visible_source(tmp_path):
    (tmp_path/'code.py').write_text('x = 1\ny = 2\n')
    job={'file':'code.py','first':1,'last':1}
    finding={'severity':'high','file':'code.py','line':1,'issue':'specific issue','evidence':'x = 1','recommendation':'fix'}
    assert validate_findings(canonical({'verdict':'needs_changes','findings':[finding]}),job,tmp_path,False)['verdict']=='needs_changes'
    for f in [{**finding,'line':2},{**finding,'evidence':'invented'},{**finding,'file':'other.py'}]:
        with pytest.raises(ValueError):validate_findings(canonical({'verdict':'needs_changes','findings':[f]}),job,tmp_path,False)
    with pytest.raises(ValueError):validate_findings(canonical({'verdict':'mock','findings':[]}),job,tmp_path,False)
    with pytest.raises(ValueError):validate_findings(canonical({'verdict':'pass','findings':[finding]}),job,tmp_path,False)
