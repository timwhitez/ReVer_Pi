"""Read-only census of a supplied S4 campaign, including stopped/non-paired runs.

This checks file identities, raw usage and ledger agreement. It does not replace
S4's full transcript auditor or establish the private provider's backend identity.
No provider client, subprocess, source execution or network operation is used.
"""
from __future__ import annotations
import csv, gzip, hashlib, io, json, os, sqlite3, stat
from collections import Counter
from pathlib import Path
from .contracts import canonical, digest, require, strict_loads, score_v2, legacy_extract_v1
from .assurance import inspect_legacy_gate, evaluate_rule
from .statistics import binomial_upper, spending_upper, group_identification

MAX_FILE = 64 * 1024 * 1024

def safe_read(root: Path, name: str, maximum: int=MAX_FILE) -> bytes:
    require(type(name)is str and name and not name.startswith('/') and '\\' not in name,'invalid_relative_path')
    parts=name.split('/');require(all(p not in ('','.','..') for p in parts),'path_escape')
    root=Path(root).absolute()
    require(all(not p.is_symlink() for p in (root,*root.parents)),'symlink_root')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        f=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            s=os.fstat(f);require(stat.S_ISREG(s.st_mode) and s.st_size<=maximum,'nonregular_or_oversized_file')
            with os.fdopen(f,'rb',closefd=False) as h:data=h.read(maximum+1)
            require(len(data)<=maximum,'file_grew');return data
        finally:os.close(f)
    finally:os.close(fd)

def load(root:Path,name:str):return strict_loads(safe_read(root,name))

def usage_counts(usage:dict) -> dict:
    require(type(usage)is dict,'usage_required')
    input_key='input_tokens' if 'input_tokens'in usage else 'prompt_tokens'
    output_key='output_tokens' if 'output_tokens'in usage else 'completion_tokens'
    inp=usage.get(input_key);out=usage.get(output_key)
    require(type(inp)is int and inp>=0 and type(out)is int and out>=0,'invalid_or_missing_usage')
    details=usage.get('input_tokens_details',usage.get('prompt_tokens_details',{}))
    require(type(details)is dict,'invalid_input_details')
    cache=details.get('cached_tokens')
    if cache is not None:require(type(cache)is int and 0<=cache<=inp,'invalid_cached_input')
    detail_out=usage.get('output_tokens_details',usage.get('completion_tokens_details',{}))
    require(type(detail_out)is dict,'invalid_output_details')
    reasoning=detail_out.get('reasoning_tokens')
    if reasoning is not None:require(type(reasoning)is int and 0<=reasoning<=out,'invalid_reasoning')
    total=usage.get('total_tokens')
    if total is not None:require(type(total)is int and total==inp+out,'total_usage_disagrees')
    return {'input_tokens':inp,'output_tokens':out,'cached_input_tokens':cache,
            'uncached_input_tokens':None if cache is None else inp-cache,
            'reasoning_tokens_included_in_output':reasoning,'observed_tokens':inp+out}

def _sum_optional(rows,key):
    return sum(r[key] for r in rows) if all(r[key] is not None for r in rows) else None

def run_census(run:Path, *, run_id:str, bucket:str, gold:dict|None=None) -> tuple[dict,list[dict]]:
    plan=load(run,'PLAN.json');summary=load(run,'summary.json');task=load(run,'TASK.json')
    require(summary['plan_sha']==digest(plan),'plan_digest_mismatch')
    require(plan['task_id']==task['task_id'] and plan['source_group']==task['source_group'],'task_identity_mismatch')
    has_boundary=(run/'boundary.json').is_file()
    require((summary['phases'][0]['state']=='boundary_captured')==has_boundary,'capture_boundary_file_mismatch')
    raw=safe_read(run,'accounting.sqlite');require(raw.startswith(b'SQLite format 3\x00'),'not_sqlite')
    for ext in ('-wal','-journal','-shm'):
        p=run/('accounting.sqlite'+ext)
        if p.exists() or p.is_symlink():
            data=safe_read(run,p.name)
            if ext!='-shm':require(len(data)==0,'nonempty_database_journal')
    # Database bytes are never opened writable. immutable is only appropriate
    # after the above stopped/empty-journal contract, not for an active ledger.
    con=sqlite3.connect((run/'accounting.sqlite').resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        con.row_factory=sqlite3.Row
        require(con.execute('PRAGMA integrity_check').fetchone()[0]=='ok','sqlite_integrity')
        attempts=[dict(r) for r in con.execute('select * from attempts order by id')]
        operations=[dict(r) for r in con.execute('select * from operations')]
    finally:con.close()
    require(len({a['op']for a in attempts})==len(attempts),'unexpected_repeated_operation')
    rows=[];phases=[]
    for phase in summary['phases']:
        name=phase['phase'];require(name in {'capture','full','projected'},'invalid_phase')
        indexes=load(run,name+'/wire/index.json') if (run/name/'wire/index.json').exists() else []
        aps=[a for a in attempts if a['cell']==name]
        # Frozen producers use cells rc7:<phase> in some revisions.
        if not aps:aps=[a for a in attempts if a['cell'].split(':')[-1]==name]
        if not aps:aps=[a for a in attempts if a['op'].split(':')[1:2]==[name]]
        require(len(indexes)==len(aps),'wire_attempt_count_mismatch')
        for item,a in zip(indexes,aps):
            req=safe_read(run,name+'/wire/'+item['request_file'])
            resp=safe_read(run,name+'/wire/'+item['response_file'])
            require(hashlib.sha256(req).hexdigest()==item['request_sha256'],'request_bytes_changed')
            require(hashlib.sha256(resp).hexdigest()==item['response_sha256'],'response_bytes_changed')
            require(item['status']==200 and item['body_complete']is True,'noncomplete_wire_requires_separate_audit')
            encoding=item.get('content_encoding','')
            require(encoding in ('','gzip'),'unsupported_encoding')
            if encoding=='gzip':
                with gzip.GzipFile(fileobj=io.BytesIO(resp)) as h:resp=h.read(MAX_FILE+1)
                require(len(resp)<=MAX_FILE,'expanded_response_limit')
            payload=strict_loads(resp);u=payload.get('usage');counts=usage_counts(u)
            ledger_usage=strict_loads(a['raw_usage']) if a['raw_usage']else None
            require(u==ledger_usage and a['actual_tokens']==counts['observed_tokens'],'ledger_wire_usage_mismatch')
            require(payload.get('model')==item['model'],'recorded_model_label_mismatch')
            rows.append({'run_id':run_id,'task_id':task['task_id'],'source_group':task['source_group'],
                         'phase':name,'op':a['op'],'model_label':payload.get('model'),'protocol':plan['provider']['protocol'],
                         **counts,'unknown':a['actual_tokens']is None})
        answer=phase.get('answer')
        strict_score=score_v2(answer,gold) if gold is not None else None
        # Exact reproduction of the historical extractor is retrospective only.
        legacy_score=None
        if gold is not None:
            extracted=legacy_extract_v1(answer)
            legacy_score=score_v2(extracted,gold)
        phase_rows=[r for r in rows if r['phase']==name]
        phases.append({'phase':name,'state':phase['state'],'reason':phase.get('reason'),
                       'requests':len(aps),'observed_tokens':sum(r['observed_tokens']for r in phase_rows),
                       'input_tokens':sum(r['input_tokens']for r in phase_rows),
                       'output_tokens':sum(r['output_tokens']for r in phase_rows),
                       'cached_input_tokens':_sum_optional(phase_rows,'cached_input_tokens'),
                       'strict_v2_descriptive':strict_score,'legacy_extracted_descriptive':legacy_score,
                       'answer_sha256':None if answer is None else hashlib.sha256(answer.encode()).hexdigest()})
    require(len(rows)==len(attempts),'unaccounted_attempts')
    totals=sum(r['observed_tokens']for r in rows)
    require(totals==summary['central_cost']['known_tokens'],'summary_tokens_mismatch')
    require(len(rows)==summary['central_cost']['attempts'],'summary_attempts_mismatch')
    count_unknown=sum(a['actual_tokens']is None for a in attempts)
    require(count_unknown==summary['central_cost']['unknown_attempts'],'unknown_count_mismatch')
    attempted={a['op']for a in attempts}
    result={'run_id':run_id,'bucket':bucket,'task_id':task['task_id'],'source_group':task['source_group'],
            'snapshot_sha256':task['snapshot_sha256'],'plan_sha256':digest(plan),
            'summary_state':summary['status'],'boundary_present':has_boundary,
            'pair_complete':summary['status']=='paired_complete','requests':len(rows),'observed_tokens':totals,
            'unknown_attempts':count_unknown,'claimed_without_attempt':sum(o['op']not in attempted for o in operations),
            'phases':phases,'max_suffix_requests':plan['max_suffix_requests'],
            'branch_order':plan['branch_order'],'model_label':plan['provider']['model'],
            'scoring_contract_note':'historical extracted scoring retained; strict_v2 is separately labelled sensitivity, not retroactive rescoring'}
    if has_boundary:
        boundary=load(run,'boundary.json');require(type(boundary)is dict and set(boundary)=={'cache_schema','payload','sha256'} and boundary['cache_schema']==1,'invalid_boundary')
        payload=boundary['payload'];require(boundary['sha256']==digest(payload),'boundary_hash_mismatch')
        result['features']={'history_bytes':len(canonical(payload['source']['messages']).encode())}
    return result,rows

def audit_campaign(delivery:Path,repo:Path) -> dict:
    taskrows=[];requestrows=[];errors=[]
    for bucket in ('s4b_paid_runs','cumulative_runs/paid','cumulative_runs/deployed'):
        for summary in sorted((delivery/'evidence'/bucket).glob('*/run/summary.json')):
            run=summary.parent;task=load(run,'TASK.json');tid=task['task_id'];gold=None
            for g in (repo/'research/s4c/data/controller'/f'{tid}.gold.json',repo/'research/s4/data/controller'/f'{tid}.gold.json'):
                if g.is_file():
                    require(hashlib.sha256(g.read_bytes()).hexdigest()==load(run,'PLAN.json')['gold_sha256'],'gold_identity_mismatch')
                    gold_object=strict_loads(g.read_bytes())
                    require(gold_object.get('schema')=='reverpi.s4.gold.v1' and gold_object.get('task_sha256')==task['task_sha256'],'gold_task_mismatch')
                    require(sorted(gold_object['expected'])==task['answer_keys'],'gold_keys_mismatch')
                    gold=gold_object['expected'];break
            try:
                row,rr=run_census(run,run_id=bucket+'/'+tid,bucket=bucket,gold=gold)
                taskrows.append(row);requestrows.extend(rr)
            except Exception as e:errors.append({'run_id':bucket+'/'+tid,'error_type':type(e).__name__,'message':str(e)})
    fit=delivery/'evidence/cumulative_runs/fit';candidate=load(fit,'RULE.json')
    cal=load(fit,'CALIB_MARGIN_30_FINAL.json');auth=load(delivery,'release_checks/RISK_MARGIN_AUTHORIZATION.json')
    diagnostic=inspect_legacy_gate(candidate,cal,auth)
    records=load(fit,'CALIBRATION_RECORDS_ALL.json')
    if isinstance(records,dict):records=records.get('records',records)
    require(type(records)is list,'calibration_records_not_list')
    require(digest(records)==cal['data_sha256'],'calibration_data_identity')
    groups=sorted(set(r['source_group']for r in records))
    rule=candidate['rule']
    selected=[r for r in records if r['features'][rule['feature']]>=rule['threshold']]
    paired=[r for r in taskrows if r['bucket']!='cumulative_runs/deployed']
    deployed=[r for r in taskrows if r['bucket']=='cumulative_runs/deployed']
    complete=[r for r in paired if r['pair_complete']]
    pair_table=[]
    for row in complete:
        phases={p['phase']:p for p in row['phases']};pfx=phases['capture']['observed_tokens']
        f=phases['full'];p=phases['projected']
        pair_table.append({'task_id':row['task_id'],'source_group':row['source_group'],
                           'full_correct_legacy':None if f['legacy_extracted_descriptive']is None else f['legacy_extracted_descriptive']['correct'],
                           'projected_correct_legacy':None if p['legacy_extracted_descriptive']is None else p['legacy_extracted_descriptive']['correct'],
                           'full_complete_observed_tokens':pfx+f['observed_tokens'],'projected_complete_observed_tokens':pfx+p['observed_tokens'],
                           'full_suffix_requests':f['requests'],'projected_suffix_requests':p['requests']})
    # Reconstruct only the observed eligible frame; it is NOT the previously
    # committed calibration sample or a representative natural source population.
    calibration_eligible=[r for r in paired if r['source_group']not in candidate['development_groups'] and r['boundary_present']]
    observed_frame={};observations=[]
    for r in calibration_eligible:
        observed_frame.setdefault(r['source_group'],[]).append(r['run_id'])
        ps={p['phase']:p for p in r['phases']}
        def endpoint(arm):
            row=ps.get(arm)
            if row is None:return None
            if row['state']=='completed' and row['legacy_extracted_descriptive'] is not None:
                return row['legacy_extracted_descriptive']['correct']
            if row['reason']=='suffix_cap':return False
            return None
        observations.append({'task_id':r['run_id'],'source_group':r['source_group'],
                             'action':evaluate_rule(candidate,r['features']),
                             'full':endpoint('full'),'projected':endpoint('projected')})
    identified=group_identification(observed_frame,observations)
    reported=load(delivery,'CAMPAIGN_SUMMARY.json')
    return {'schema':'reverpi.s5.campaign-census.v1' ,'scope':'all submitted run summaries, not all possible source tasks',
            'runs':taskrows,'requests':requestrows,'errors':errors,'historical_gate_audit':diagnostic,
            'totals':{'runs':len(taskrows),'requests':len(requestrows),'reported_tokens':sum(r['observed_tokens']for r in requestrows),
                      'unknown_attempts':sum(r['unknown']for r in requestrows),'paired_or_capture_runs':len(paired),
                      'eligible_runs':sum(r['boundary_present']for r in paired),'complete_pairs':len(complete),
                      'deployed_runs':len(deployed),'unique_source_groups':len(set(r['source_group']for r in paired)),
                      'raw_response_model_labels':dict(Counter(r['model_label']for r in requestrows))},
            'complete_pair_table':pair_table,
            'calibration_selection':{'submitted_complete_rows':len(records),'submitted_groups':groups,
                                     'projected_selected_rows':len(selected),'projected_selected_tasks':[r['id']for r in selected],
                                     'observed_eligible_nontraining_runs':len(calibration_eligible),
                                     'observed_eligible_nontraining_groups':sorted(set(r['source_group']for r in calibration_eligible)),
                                     'fixed_n_zero_harm_upper':binomial_upper(0,len(groups),cal['alpha']),
                                     'alpha_spending_at_same_n':spending_upper(0,len(groups),cal['alpha']),
                                     'prospective_authority_established':False,
                                     'posthoc_observed_eligible_frame':identified,
                                     'posthoc_selected_outcomes':observations},
            'boundary_definition_discrepancy':{'reported_paired_boundaries':reported['experiment_totals']['eligible_boundaries'],
               'verified_paired_boundaries':sum(r['boundary_present']for r in paired),
               'reported_deployed_boundaries':reported['deployed']['boundaries'],
               'verified_deployed_boundaries':sum(r['boundary_present']for r in deployed),
               'reason':'non-no_eligible status incorrectly includes capture-stop without boundary'},
            'new_model_calls':0,'source_modified':False,'historical_scores_modified':False,'private_usd':None}

def write_outputs(result:dict,out:Path):
    require(not out.exists(),'output_exists');out.mkdir(parents=True)
    (out/'census.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    for name,rows in [('request_usage.csv',result['requests']),('complete_pairs.csv',result['complete_pair_table'])]:
        with (out/name).open('x',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]) if rows else ['empty']);writer.writeheader();writer.writerows(rows)
    # No raw answers, request bodies, URLs, headers or credentials in outputs.
