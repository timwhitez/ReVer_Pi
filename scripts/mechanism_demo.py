#!/usr/bin/env python3
"""Zero-model engineering witnesses: ordinary evidence omission and fresh execution.

No learned-reader results, public benchmarks or semantic-sufficiency claims.
The raw historical observation intentionally contains the random fact. A separate
evaluator-only answer file is not sent to a model; this is NOT an impossibility proof.
"""
from __future__ import annotations
import argparse,asyncio,hashlib,json,secrets,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.config import CompressionConfig
from reverpi.data import Checkpoint,Question
from reverpi.memory import Archive,Compressor,Record
from reverpi.revalidation import VerifierSpec,execute_verifier
from reverpi.paired_interventions import freeze_intervention
from reverpi.util import canonical,bytes_digest,atomic_write


async def demo(out:Path):
    out.mkdir(parents=True,exist_ok=False,mode=0o700)
    token=secrets.token_hex(16)
    records=[Record(id='goal',kind='goal',text='Complete the audit.'),
       Record(id='constraint',kind='constraint',text='Do not access the network.'),
       Record(id='receipt',kind='observation',text='Audit receipt\n'+'progress\n'*800+'TOKEN='+token+'\n'+'progress\n'*800),
       Record(id='tail',kind='note',text='Await the next query.')]
    cp=Checkpoint(id='owned-random-receipt',split='search',source_group='owned-delayed-query-fixture',
       provenance={'template':'rc3-owned-mechanism-v1'},task='Audit the historical receipt.',records=records,
       questions=[Question(id='historical_token',category='E',text='What was the TOKEN in the audit receipt?')],synthetic=True)
    atomic_write(out/'checkpoint.json',canonical(cp.model_dump()))
    gold=out/'EVALUATOR_ONLY_GOLD.json';gold.write_text(canonical({'answers':{'historical_token':[token]}}));gold.chmod(0o600)
    rows=[]
    cfg=CompressionConfig(memory_bytes=2048,recent_records=1,recovery_chars=20000)
    for method in ['mask','rever_lite','tail','lexical']:
        folder=out/method;folder.mkdir();archive=Archive(folder/'archive.sqlite',cfg)
        mem=await Compressor(cfg,archive).compress(records,method,cell='development-demo')
        h=mem.details['handles']['receipt'];restored=archive.recover('development-demo','witness',handle=h,chars=20000)
        atomic_write(folder/'memory.json',canonical(mem.model_dump()))
        parent=freeze_intervention(folder/'parent.json',cp,mem,parent_cost={'observed_tokens':0,'model_calls':0},expected_generated=False)
        row={'method':method,'compressed':True,'memory_bytes':mem.bytes,'parent_sha256':parent,
            'token_literal_present':token in mem.text,'exact_archive_record_matches':restored['text']==records[2].text,
            'protected_contract_preserved':all(r.text in mem.text for r in records if r.kind in {'goal','constraint'}),
            'semantic_certified':mem.semantic_certified}
        if row['token_literal_present'] or not row['exact_archive_record_matches']:raise RuntimeError('Mechanism witness failed')
        rows.append(row)
    workspace=out/'task_workspace';workspace.mkdir()
    (workspace/'check.py').write_text('from pathlib import Path\nassert Path("state.txt").read_text() == "A"\n')
    (workspace/'state.txt').write_text('A')
    spec=VerifierSpec('check-state',(sys.executable,'-B','check.py'),('check.py','state.txt'))
    before=execute_verifier(spec,workspace)
    (workspace/'state.txt').write_text('B')
    after=execute_verifier(spec,workspace)
    if not before['passed'] or after['passed']:raise RuntimeError('Fresh-verification witness failed')
    receipt={'schema':1,'verifiers':[spec.__dict__]};reg=out/'verifiers.json';reg.write_text(canonical(receipt))
    result={'schema':1,'experiment_kind':'engineering_mechanism_witness_NOT_LLM_benchmark','paid_model_calls':0,
       'compressed_methods':rows,'historic_receipt':before,'fresh_receipt':after,
       'registry_sha256':bytes_digest(reg.read_bytes()),'reader_policy_executed':False,
       'domain_generalization_proven':False,'independent_benchmark':False}
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({'methods':rows,'past_pass':before['passed'],'current_pass':after['passed'],'model_calls':0}))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    asyncio.run(demo(p.parse_args().out))
if __name__=='__main__':main()
