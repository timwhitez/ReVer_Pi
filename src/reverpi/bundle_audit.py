"""Read-only archive audits. No network calls, database repairs or gate promotion.

An extracted archive is a snapshot, not access to the originating host. Hashes
check consistency, not authorship. Missing evidence is never treated as zero.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import json
from .research_audit import readonly_db, audit_ledger
from .util import bytes_digest, digest, strict_json_loads, safe_id


def contained(root: Path, relative: str) -> Path:
    root = root.resolve(strict=True)
    p = Path(relative)
    if p.is_absolute() or '..' in p.parts or not p.parts:
        raise ValueError('Expected a contained relative path')
    result = root.joinpath(p)
    if any(root.joinpath(*p.parts[:i]).is_symlink() for i in range(1,len(p.parts)+1)):
        raise ValueError('Symlink evidence is not admitted')
    if not result.resolve().is_relative_to(root):
        raise ValueError('Evidence escaped its archive root')
    return result


def load(path: Path):
    return strict_json_loads(path.read_bytes())


def audit_delivery(root: Path, manifest_name: str = 'MANIFEST_DELIVERY.json') -> dict:
    manifest = load(contained(root,manifest_name))
    table = manifest.get('files')
    if not isinstance(table,dict) or manifest.get('file_count') != len(table):
        raise ValueError('Malformed delivery manifest')
    missing, changed = [], []
    for name, expected in table.items():
        p = contained(root,name)
        if not p.is_file(): missing.append(name)
        elif bytes_digest(p.read_bytes()) != expected: changed.append(name)
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
    return {'manifest_entries':len(table),'missing':missing,'changed':changed,
        'unlisted_files':sorted(actual-set(table)-{manifest_name}),
        'all_listed_hashes_match':not missing and not changed,
        'authenticity_proven':False,'read_only':True}


def audit_review(root: Path, directory: Path) -> dict:
    """Reconstruct available frozen jobs; do not invent missing source chunks.

    Works on the RC1 archive without restoring its omitted build metadata. The
    reconstructed payload check uses the frozen profile, not provider.local.yaml.
    No credentials are read from the environment or returned in the report.
    """
    from .review import review_jobs, review_messages, validate_findings
    from .config import Provider
    from .protocols import build_request
    report, records = load(contained(directory,'review_manifest.json')), load(contained(directory,'reviews.json'))
    if not isinstance(records,list): raise ValueError('Expected review record array')
    with readonly_db(contained(directory,'ledger.sqlite'), immutable_snapshot=True) as db:
        meta = {r['key']:strict_json_loads(r['value']) for r in db.execute('SELECT key,value FROM meta')}
        operations = [dict(r) for r in db.execute('SELECT op,cell,payload_sha,state,error FROM operations ORDER BY created')]
        attempts = [dict(r) for r in db.execute('SELECT id,op,state,reserve_tokens,actual_tokens,error_kind FROM attempts ORDER BY id')]
    frozen = meta['review_source']; frozen_sha = digest(frozen)
    profiles = [v for k,v in meta.items() if k.startswith('provider:')]
    if len(profiles)!=1: raise ValueError('Expected exactly one frozen review profile')
    profile = Provider.model_validate(profiles[0])
    missing, changed = [], []
    for name, expected in frozen.items():
        p=contained(root,name)
        if not p.is_file(): missing.append(name)
        elif bytes_digest(p.read_bytes())!=expected: changed.append(name)
    # Reconstruct only chunks of byte-identical source. Missing files stay unknown.
    _, available = review_jobs(root, meta['review_rounds'])
    available = [j for j in available if j['file'] in frozen and j['file'] not in missing and j['file'] not in changed]
    for j in available: j['code_sha']=frozen_sha
    op_map={o['op']:o for o in operations}; identified={}; residual=[]
    for job in available:
        op='review_'+digest(job)[:32]
        if op in op_map: identified[op]=job
        else: residual.append({'op':op,**{k:v for k,v in job.items() if k!='source'}})
    visited_keys={tuple(r.get(k) for k in ('file','round','first','last','code_sha')) for r in records}
    unvisited_preclaimed=[]
    for op, job in identified.items():
        if tuple(job[k] for k in ('file','round','first','last','code_sha')) not in visited_keys:
            unvisited_preclaimed.append({'op':op,'state':op_map[op]['state'],
                **{k:v for k,v in job.items() if k!='source'},
                'attempt_count':sum(a['op']==op for a in attempts)})
    terminal = None
    if records:
        row=records[-1]
        match=[j for j in available if all(row.get(k)==j.get(k) for k in ('file','round','first','last','code_sha'))]
        if len(match)==1:
            job=match[0];op='review_'+digest(job)[:32]
            path,body=build_request(profile,review_messages(job),None)
            # Preserve the serialized frozen fingerprint: model validation can turn
            # default int timeouts into floats (10 -> 10.0), changing JSON hashes.
            computed=digest({'profile':digest(profiles[0]),'path':path,'body':body})
            stored=op_map.get(op)
            terminal={'visited_index':len(records),'op':op,'file':job['file'],'round':job['round'],
                'stored_state':stored['state'] if stored else None,
                'payload_sha_matches':stored is not None and stored['payload_sha']==computed,
                'attempts':[a for a in attempts if a['op']==op],
                'reported_error':row.get('error',{}).get('kind'), 'automatic_resend_authorized':False}
    revalidated,unverifiable,invalid=[],[],[]
    jobs_by_key={tuple(j[k] for k in ('file','round','first','last','code_sha')):j for j in available}
    for row in records:
        if row.get('status')!='complete':continue
        key=tuple(row.get(k) for k in ('file','round','first','last','code_sha'))
        descriptor={k:row.get(k) for k in ('file','round','first','last')}
        if key not in jobs_by_key:
            unverifiable.append(descriptor);continue
        try:
            from .util import canonical
            validate_findings(canonical(row.get('result')),jobs_by_key[key],root,bool(profiles[0]['mock']))
        except (ValueError,TypeError,KeyError):invalid.append(descriptor)
        else:revalidated.append(descriptor)
    verdicts=Counter(r['result']['verdict'] for r in records if r.get('result'))
    errors=Counter(r.get('error',{}).get('kind','unspecified') for r in records if r.get('status')!='complete')
    findings=[f for r in records if r.get('result') for f in r['result']['findings']]
    return {'schema':'reverpi.review-audit.v1','read_only':True,
        'records_checksum_matches':report.get('review_records_sha')==digest(records),
        'source_sha_matches_report':report.get('source_sha')==frozen_sha,
        'frozen_source_files':len(frozen),'missing_source_files':missing,'changed_source_files':changed,
        'exact_frozen_tree_present':not missing and not changed,
        'reported_planned_jobs':report['planned_jobs'],'visited_jobs':len(records),
        'unvisited_jobs':report['planned_jobs']-len(records),
        'valid_records':sum(r.get('status')=='complete' for r in records),
        'available_records_revalidated':len(revalidated),
        'completed_records_unverifiable':unverifiable,'completed_records_invalid':invalid,
        'verdict_counts':dict(verdicts),'error_counts':dict(errors),
        'findings':len(findings),'severity_counts':dict(Counter(f['severity'] for f in findings)),
        'operation_states':dict(Counter(o['state'] for o in operations)),
        'attempt_states':dict(Counter(a['state'] for a in attempts)),
        'terminal_job':terminal,'available_unclaimed_jobs':residual,
        'unvisited_preclaimed_jobs':unvisited_preclaimed,
        'frozen_provider_sha':digest(profiles[0]),
        'normalized_provider_sha':digest(profile.model_dump()),
        'frozen_provider_sha_matches_report':digest(profiles[0])==report.get('provider_sha'),
        'cost':audit_ledger(contained(directory,'ledger.sqlite'), immutable_snapshot=True)['totals'],
        'all_jobs_valid':False if errors or len(records)!=report['planned_jobs'] else report.get('all_jobs_valid'),
        'eligible_for_human_acceptance':False,
        'notes':['Valid output does not mean a pass verdict or an adjudicated finding.',
                 'An ambiguous op is quarantined, not reset, replayed or assigned zero cost.',
                 'Residual jobs are diagnostic, not a paid execution authorization.',
                 'Missing source files prevent exact old-generation resumption.']}


def session_evidence(path: Path) -> dict:
    if not path.is_file():
        return {'available':False,'compactions':None,'recovery_tool_calls':None,
                'assistant_errors':None,'transport_errors':None}
    calls=Counter();errors=[];compactions=0;messages=0
    for line in path.read_text(encoding='utf-8').splitlines():
        item=strict_json_loads(line)
        if not isinstance(item,dict):raise ValueError('Non-object session entry')
        compactions += item.get('type')=='compaction'
        message=item.get('message',{})
        if not isinstance(message,dict):raise ValueError('Non-object session message')
        if message:messages+=1
        if message.get('role')=='assistant':
            content=message.get('content',[])
            if isinstance(content,list):
                for b in content:
                    if isinstance(b,dict) and b.get('type')=='toolCall':calls[str(b.get('name'))]+=1
            if message.get('stopReason') in {'error','aborted'}:
                text=message.get('errorMessage','')
                errors.append({'stopReason':message['stopReason'],
                    'fetch_failed':isinstance(text,str) and 'fetch failed' in text,
                    'other_side_closed':isinstance(text,str) and 'other side closed' in text})
    return {'available':True,'compactions':compactions,'recovery_tool_calls':calls['recover_evidence'],
        'tool_calls':dict(calls),'assistant_errors':len(errors),'messages':messages,
        'transport_errors':sum(e['fetch_failed'] for e in errors),'error_signatures':errors,
        'recovery_correctness_verified':False}


def audit_acceptance(directory: Path) -> dict:
    report=load(contained(directory,'native_acceptance.json'));rows=[]
    for row in report['methods']:
        method=safe_id(row['method'])
        rows.append({'method':method,'reported_accepted':row['accepted'],
            'reported_compaction_committed':row.get('compaction_committed'),
            **session_evidence(contained(directory,f'{method}/session.jsonl'))})
    custom=[r for r in rows if r['method'] not in {'pi_original','pi_native'}]
    return {'schema':'reverpi.acceptance-audit.v1','methods':rows,
        'read_only':True,'historical_acceptance_changed':False,
        'all_custom_methods_invoked_recovery':None if not custom else all(
            type(r.get('recovery_tool_calls')) is int and r['recovery_tool_calls']>0 for r in custom),
        'note':'A remembered nonce without a recovery tool call does not certify the recovery path.'}


def audit_native_run(directory: Path) -> dict:
    manifest=load(contained(directory,'manifest.json'));rows=load(contained(directory,'results.json'));output=[]
    grid={g['cell']:g for g in manifest['grid']}
    expected=set(grid)
    if len(expected)!=len(manifest['grid']) or len(rows)!=len(expected) or {r['cell'] for r in rows}!=expected:
        raise ValueError('Duplicate, missing or unplanned native cells')
    for row in rows:
        cell=safe_id(row['cell']);jobroot=contained(directory,f'jobs/{cell}')
        checksum=row.get('row_sha256')==digest({k:v for k,v in row.items() if k!='row_sha256'})
        grid_match=all(row.get(k)==grid[cell].get(k) for k in
            ('method','task_id','source_group','repeat','task_sha'))
        paths=[contained(jobroot,str(p.relative_to(jobroot))) for p in jobroot.rglob('result.json')]
        matches=[p for p in paths if bytes_digest(p.read_bytes())==row.get('official_result_sha')]
        reward=None;se={'available':False,'compactions':None,'recovery_tool_calls':None,'transport_errors':None}
        if len(matches)==1:
            official=load(matches[0]);reward=(official.get('verifier_result') or {}).get('rewards',{}).get(row.get('reward_key','reward'))
            logs=list(matches[0].parent.rglob('session.jsonl'))
            if len(logs)==1:se=session_evidence(contained(jobroot,str(logs[0].relative_to(jobroot))))
        output.append({'cell':cell,'task_id':row['task_id'],'method':row['method'],
            'source_group':row['source_group'],'success_reported':row.get('success'),
            'official_reward':reward,'row_checksum_matches':checksum,'frozen_grid_matches':grid_match,
            'official_hash_unique':len(matches)==1,
            'reward_matches_record':len(matches)==1 and reward==row.get('official_reward'),
            'known_tokens':row.get('cost',{}).get('known_tokens'),
            'accounted_tokens':row.get('cost',{}).get('accounted_tokens'),**se})
    return {'schema':'reverpi.native-audit.v1','read_only':True,'cells':output,
        'summary':{'cells':len(output),'successes_reported':sum(r['success_reported'] is True for r in output),
            'cells_with_transport_errors':sum((r.get('transport_errors') or 0)>0 for r in output),
            'unobserved_sessions':sum(not r['available'] for r in output),
            'compactions_observed':sum(r.get('compactions') or 0 for r in output),
            'all_result_identities_match':all(r['row_checksum_matches'] and r['official_hash_unique'] and r['reward_matches_record'] and r['frozen_grid_matches'] for r in output)},
        'method_causality_proven':False,'network_root_cause_proven':False,
        'note':'Observation absence is only evaluated for available logs; an HTTP status is not a packet trace.'}
