"""Independent semantic checks for the fixed offline profile, not future generic steps.

Exit success from a producer is insufficient. These checks read its concrete
artifact contents and preserve all zero-model / no-certification boundaries.
"""
from pathlib import Path
from hashlib import sha256


def require(value: bool, message: str) -> None:
    if not value:
        raise ValueError(message)


def validate_profile_artifacts(out: Path) -> dict:
    from reverpi.util import contained_regular_file, strict_json_loads
    def read(name):
        result = strict_json_loads(contained_regular_file(out, name).read_bytes())
        require(isinstance(result, dict), 'Artifact must be an object: '+name)
        return result
    def receipt(r, passed):
        require(isinstance(r,dict) and r.get('schema')=='reverpi.verification-receipt.v1', 'Receipt schema')
        require(r.get('status')=='completed' and r.get('passed') is passed, 'Receipt state')
        require(type(r.get('returncode')) is int and (r['returncode']==0)==passed, 'Receipt returncode')
        require(isinstance(r.get('output'),str) and sha256(r['output'].encode()).hexdigest()==r.get('output_sha256'), 'Receipt output hash')
        require(r.get('dependency_completeness_certified') is False and r.get('decision_sufficiency_certified') is False, 'Overstated receipt')
    checked=[]
    h=read('historical/report.json')
    require(h.get('paid_ready') is False and h.get('real_model_calls')==0, 'Historical authority')
    require(h.get('budget_expected_blocks')==['dev-01','dev-02','review'], 'Missing budget negative control')
    gold=h.get('gold_checks')
    require(isinstance(gold,list) and len(gold)==2, 'Gold coverage')
    for x,slot,ok in zip(gold,['dev-01','dev-02'],[False,True]):
        require(x.get('slot')==slot and x.get('schema_valid') is ok and x.get('certificate_written') is ok
            and x.get('official_gold_modified') is False, 'Gold negative control mismatch')
    checked.append('historical')
    p=read('public-tests/report.json');rows=p.get('rows')
    require(p.get('real_model_calls')==0 and p.get('original_templates_modified') is False
        and p.get('official_results_regraded') is False, 'Historical mutation')
    require(isinstance(rows,list) and len(rows)==2, 'Public test coverage')
    for x,slot,ok in zip(rows,['dev-01','dev-02'],[True,False]):
        require(x.get('slot')==slot and x.get('observed_passed') is ok and x.get('expected_passed') is ok
            and x.get('original_template_modified') is False and x.get('original_registry_bytes_unchanged') is True, 'Public test outcome')
    checked.append('public-tests')
    c=read('campaign/report.json');rows=c.get('matrix_rows')
    require(c.get('real_model_calls')==0 and c.get('whole_tree_review_certified') is False
        and c.get('paid_authority_granted') is False and c.get('matrix_cells')==16, 'Campaign scope')
    require(isinstance(rows,list) and len(rows)==4 and {(x.get('protocol'),x.get('slot')) for x in rows}
        =={(p,s) for p in ('chat_completions','responses') for s in ('dev-01','dev-02')}, 'Campaign identity')
    for x in rows:
        require(x.get('completed')==4 and x.get('attempts')==4 and x.get('cached_replay_new_calls')==0
            and x.get('answers_are_mock_unknown') is True and x.get('quality_claim') is False, 'Campaign replay contract')
    require(c.get('mock_scoped_review_jobs')==2, 'Scoped review coverage')
    checked.append('campaign')
    c=read('review-cache/report.json');n=c.get('jobs')
    require(type(n) is int and n>0 and c.get('completed')==n and c.get('cached_replays')==n
        and c.get('cache_added_accounted_tokens')==0 and c.get('cache_added_attempts')==0
        and c.get('formal_gate_rejects_mock') is True and c.get('real_model_calls')==0, 'Mock review cache')
    checked.append('review-cache')
    c=read('native/report.json');rows=c.get('cells')
    require(c.get('mock_provider') is True and c.get('paid_model_calls')==0
        and c.get('native_gate_eligible') is False and c.get('harbor_path_exercised') is False, 'Native claims')
    require(isinstance(rows,list) and len(rows)==4 and {(x.get('method'),x.get('phase')) for x in rows}
        =={(m,p) for m in ('pi_original','mask') for p in ('A','B')}, 'Native coverage')
    for x in rows:
        ok=x['phase']=='A'
        require(x.get('status')=='completed' and x.get('expected_passed') is ok
            and x.get('observed_passed') is ok and x.get('new_receipts')==1, 'Native execution outcome')
        receipt(x.get('receipt'),ok)
    checked.append('native')
    c=read('network/matrix.json');rows=c.get('rows')
    require(c.get('route')=='local_direct_only' and c.get('sidecar_tested') is False and c.get('model_calls')==0, 'Network scope')
    require(isinstance(rows,list) and len(rows)==4 and {(x.get('delay_seconds'),x.get('mode')) for x in rows}
        =={(d,m) for d in (0,25) for m in ('headers','body')}, 'Network coverage')
    require(all(x.get('ok') is True and x.get('server_completion_matching') is True and x.get('exit_code')==0
        and x.get('phase')=='complete' for x in rows), 'Network complete-body contract')
    checked.append('network')
    c=read('mechanism/results.json');rows=c.get('compressed_methods')
    require(c.get('paid_model_calls')==0 and c.get('reader_policy_executed') is False
        and c.get('independent_benchmark') is False, 'Mechanism scope')
    require(isinstance(rows,list) and len(rows)==4 and {x.get('method') for x in rows}=={'mask','rever_lite','tail','lexical'}, 'Mechanism coverage')
    for x in rows:
        require(x.get('compressed') is True and x.get('token_literal_present') is False
            and x.get('exact_archive_record_matches') is True and x.get('protected_contract_preserved') is True
            and x.get('semantic_certified') is False, 'Mechanism witness')
    receipt(c.get('historic_receipt'),True);receipt(c.get('fresh_receipt'),False)
    checked.append('mechanism')
    return {'schema':1,'fixed_profile_families_verified':checked,'all_future_steps_certified':False,
        'new_model_calls':0,'independent_model_review_passed':False}
