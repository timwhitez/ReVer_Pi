"""Acceptance is derived from raw evidence, not a boolean someone can mistype."""
import asyncio
import json
from pathlib import Path
import pytest
from reverpi import review
from reverpi.acceptance import check_acceptance
from reverpi.config import Provider, Budget
from reverpi.errors import LabError
from reverpi.statistics import analyze
from reverpi.util import canonical, digest

@pytest.mark.parametrize('field,value',[
 ('mock',None),('mock',0),('eligible_for_native_gate','false'),
 ('methods',[{'method':'mask','accepted':'false'}]),
 ('methods',[{'method':'mask','accepted':True},{'method':'mask','accepted':False}]),
])
def test_native_gate_rejects_truthy_or_missing_attestation(field,value):
    obj={'source_sha':'code','provider_sha':'p','mock':False,'eligible_for_native_gate':True,
         'methods':[{'method':'mask','accepted':True}]}
    obj[field]=value
    with pytest.raises(LabError):check_acceptance(obj,'code','p',['mask'])


def test_no_shared_tasks_cannot_have_a_mcnemar_pvalue():
    rows=[{'task_id':'a','source_group':'a','method':'full','repeat':0,'success':True},
          {'task_id':'b','source_group':'b','method':'mask','repeat':0,'success':True}]
    cmp=analyze(rows,['full','mask'])['comparisons'][0]
    assert cmp['paired_tasks']==0 and cmp['independent_mcnemar_p'] is None

@pytest.mark.asyncio
async def test_review_gate_rejects_flipped_mock_and_modified_records(tmp_path):
    root=tmp_path/'srcroot';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('X=1\n')
    out=tmp_path/'review';p=Provider(mock=True,model='mock-reasoner')
    report=await review.run_reviews(root,p,Budget(min_free_disk_bytes=0),out,rounds=3)
    checker=getattr(review,'check_review',None)
    assert checker is not None, 'Formal gates must inspect evidence, not only one precomputed eligibility flag'
    with pytest.raises(LabError):checker(out/'review_manifest.json',root)
    report.update(mock=False,eligible_for_human_acceptance=True)
    (out/'review_manifest.json').write_text(canonical(report))
    with pytest.raises(LabError):checker(out/'review_manifest.json',root)
    records=json.loads((out/'reviews.json').read_text());records[0]['result']={'verdict':'pass','findings':[]}
    (out/'reviews.json').write_text(canonical(records))
    with pytest.raises(LabError):checker(out/'review_manifest.json',root)

@pytest.mark.asyncio
async def test_review_evidence_consistency_positive_then_missing_job(tmp_path,provider,monkeypatch):
    # Live-marked profile and HTTP mock transport are a TEST DOUBLE, not a genuine live certificate.
    import httpx
    provider=provider.model_copy(update={"mock":False})
    monkeypatch.setenv(provider.api_key_env,"local-test-double-not-a-key")
    from test_transport import raw
    root=tmp_path/'srcroot';(root/'src').mkdir(parents=True);(root/'src/x.py').write_text('X=1\n')
    out=tmp_path/'review'
    response=raw(provider);response['choices'][0]['message']['content']=canonical({'verdict':'pass','findings':[]})
    response['choices'][0]['message'].pop('tool_calls',None)
    report=await review.run_reviews(root,provider,Budget(min_free_disk_bytes=0),out,rounds=3,
               transport=httpx.MockTransport(lambda r:httpx.Response(200,json=response)))
    checker=getattr(review,'check_review',None)
    assert checker is not None
    assert checker(out/'review_manifest.json',root)['all_jobs_valid'] is True
    records=json.loads((out/'reviews.json').read_text());records.pop()
    (out/'reviews.json').write_text(canonical(records))
    report.update(review_records_sha=digest(records))
    (out/'review_manifest.json').write_text(canonical(report))
    with pytest.raises(LabError):checker(out/'review_manifest.json',root)
