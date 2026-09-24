"""Deterministic regression checks; none are model capability measurements."""
import asyncio
import hashlib
import json
import sys
from pathlib import Path
import httpx
import pytest
from reverpi.config import CompressionConfig, StudyConfig, Budget
from reverpi.memory import Archive
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.probe import probe
from reverpi.review import run_reviews
from reverpi.revalidation import execute_verifier, VerifierSpec
from reverpi.paired_interventions import Arm, intervention_cell, run_arm, freeze_intervention
from reverpi.util import canonical
from test_transport import raw
from test_rc3_mechanisms import records, ScriptedReader
from reverpi.memory import Compressor
from reverpi.data import Checkpoint, Question

@pytest.mark.parametrize('mode',['head','match'])
def test_recovery_navigation_literal_and_centered(tmp_path,mode):
    a=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode=mode))
    text='证据🙂'*500+'AlPhA%_id=0123456789'+' END'*500
    handle=a.put('c',text)
    r=a.recover('c','q',query='alpha%_id',chars=100)
    hit=r['matches'][0]
    assert hit['handle']==handle
    if mode=='head':
        assert 'AlPhA%_id' not in hit['excerpt'] and 'start' not in hit
    else:
        assert hit['match_start']==text.index('AlPhA%_id')
        assert 'AlPhA%_id' in hit['excerpt']
        assert text[hit['start']:hit['end']]==hit['excerpt']
        assert r['offset_unit']=='unicode_codepoints'
    assert a.recover('c','q',query='alpha%_id',chars=100)==r
    assert a.recover('other','q',query='alpha%_id',chars=100)['matches']==[]

@pytest.mark.parametrize('chars',[1,2,7,100,6000])
def test_search_never_expands_content_quota(tmp_path,chars):
    a=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode='match'))
    for i in range(8):a.put('c',str(i)*50+'needle'+str(i)*100)
    r=a.recover('c','q',query='needle',chars=chars)
    assert len(r['matches'])==5
    assert sum(len(x['excerpt']) for x in r['matches'])<=chars
    assert all(x['match_start']==50 for x in r['matches'])


def test_search_change_does_not_replay_stale_receipt(tmp_path):
    p=tmp_path/'a.sqlite';a=Archive(p,CompressionConfig());a.put('c','x'*500+'needle')
    a.recover('c','q',query='needle')
    b=Archive(p,CompressionConfig(recovery_search_mode='match'))
    with pytest.raises(LabError,match='different arguments'):b.recover('c','q',query='needle')


def test_search_detects_corruption(tmp_path):
    a=Archive(tmp_path/'a.sqlite',CompressionConfig(recovery_search_mode='match'));a.put('c','needle')
    with a.connect() as db:db.execute("UPDATE blobs SET content='needle CORRUPTED'")
    with pytest.raises(LabError,match='content hash'):a.recover('c','q',query='needle')

@pytest.mark.parametrize('options',[{'response_heartbeat_seconds':1.0},
 {'early_response_headers':True,'response_heartbeat_seconds':0.001}])
def test_heartbeat_requires_explicit_valid_profile(options):
    with pytest.raises(ValueError):StudyConfig(**options)

@pytest.mark.asyncio
@pytest.mark.parametrize('protocol',['chat_completions','responses'])
async def test_empty_text_probe_cannot_certify(tmp_path,provider,protocol):
    p=provider.model_copy(update={'protocol':protocol})
    r=await probe(p,Budget(),tmp_path/'probe',transport=httpx.MockTransport(lambda _:httpx.Response(200,json=raw(p,'   '))))
    assert r['compatible_for_this_probe'] is False
    assert r['error']['kind'] in {'probe_text_contract','empty_completion'}
    assert r['cost']['attempts']==1

@pytest.mark.asyncio
async def test_review_stops_at_source_change_without_spending_remaining_jobs(tmp_path,provider):
    root=tmp_path/'code';(root/'src').mkdir(parents=True);file=root/'src/code.py';file.write_text('a = 1\n')
    calls=0
    def handler(_):
        nonlocal calls;calls+=1;file.write_text('a = 2\n')
        return httpx.Response(200,json=raw(provider,'{"verdict":"pass","findings":[]}'))
    r=await run_reviews(root,provider,Budget(),tmp_path/'r',rounds=3,transport=httpx.MockTransport(handler))
    assert calls==1 and r['halt_reason']=='source_changed'
    assert r['attempted_jobs']==1 and r['unvisited_jobs']==2
    assert not r['all_jobs_valid'] and not r['source_unchanged']
    assert r['cost']['known_tokens']==18

@pytest.mark.asyncio
@pytest.mark.parametrize('exception',[LabError('test_failure','failure'),RuntimeError('SECRET-DO-NOT-ECHO')])
async def test_compactor_late_errors_have_body_and_preserve_revision(tmp_path,provider,monkeypatch,exception):
    async def fail(*args,**kwargs):raise exception
    monkeypatch.setattr('reverpi.gateway.Compressor.compress',fail)
    app=create_app(provider,StudyConfig(methods=['mask'],early_response_headers=True),tmp_path/'g')
    token=app.state.sessions.create('one','mask')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as c:
        r=await c.post('/compact',headers={'Authorization':'Bearer '+token},json={
            'op':'first','expected_revision':0,'records':[{'id':'r','kind':'goal','text':'g'}]})
        assert r.status_code==200
        assert r.json()['error']['kind']==('test_failure' if isinstance(exception,LabError) else 'gateway_internal')
        assert 'SECRET' not in r.text
    assert app.state.sessions.get('one')['revision']==0
    await app.state.client.close()


def test_verification_receipt_bound_to_invocation(tmp_path):
    (tmp_path/'check.py').write_text('print("current")\n')
    spec=VerifierSpec('check',(sys.executable,'check.py'),('check.py',))
    r=execute_verifier(spec,tmp_path,invocation_nonce='a'*64)
    assert r['invocation_nonce']=='a'*64
    assert r['workspace_path_sha256']==hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()
    assert not r['receipt_is_cryptographic_attestation']
    with pytest.raises(ValueError):execute_verifier(spec,tmp_path,invocation_nonce='wrong')


def test_trial_identity_separates_repetitions_without_separate_budgets():
    arm=Arm('recover',3,0,6)
    assert intervention_cell('a'*64,arm,trial_id='rep1')!=intervention_cell('a'*64,arm,trial_id='rep2')
    assert intervention_cell('a'*64,arm)==intervention_cell('a'*64,arm)
    with pytest.raises(ValueError):intervention_cell('a'*64,arm,trial_id='../retry')

@pytest.mark.asyncio
async def test_two_replicates_in_one_ledger_and_same_memory(tmp_path,ledger):
    cfg=CompressionConfig(memory_bytes=2048,recent_records=1,recovery_search_mode='match')
    rs=records('secret');mem=await Compressor(cfg,Archive(tmp_path/'parent.sqlite',cfg)).compress(rs,'mask',cell='p')
    cp=Checkpoint(id='q',split='search',source_group='owned-one-cluster',provenance={'template':'test'},task='Retrieve the record.',records=rs,
        questions=[Question(id='id',text='What was recorded?',category='E')],synthetic=True)
    p=tmp_path/'parent.json';sha=freeze_intervention(p,cp,mem,parent_cost={})
    out=[]
    for trial in ['rep1','rep2']:
        out.append(await run_arm(p,sha,Arm('recover',3),ScriptedReader(ledger,'recover'),tmp_path/trial,archive_config=cfg,trial_id=trial))
    assert out[0]['cell']!=out[1]['cell']
    assert out[0]['memory_text_sha256']==out[1]['memory_text_sha256']
    plans=[json.loads((tmp_path/t/'plan.json').read_text()) for t in ['rep1','rep2']]
    assert all(p['archive_config']['recovery_search_mode']=='match' for p in plans)
