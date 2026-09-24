"""S4 independent raw-artifact check; adapted from frozen verify_paired_prefix.

Quality is computed separately with a controller gold file. Passing this check
means internal consistency under the single-operator threat model, not model
competence, independent research-team approval, or complete supply-chain trust.
"""
from __future__ import annotations
import copy,hashlib
from pathlib import Path
from dataclasses import asdict
from reverpi.paired_prefix import decode_recorded_body,paired_tools
from reverpi.protocols import Message,build_request,parse_completion,normalize_usage
from reverpi.research_audit import readonly_db,audit_ledger
from reverpi.util import canonical,digest,strict_json_loads,unseal_cache,contained_regular_file
from online_projection_audit import check_record,transcript
from .contracts import SourcePlan,tree_manifest,load_task,render_prompt

def need(ok, message):
    if not ok: raise ValueError(message)


def load(root, name):
    return strict_json_loads(contained_regular_file(root, name).read_bytes())


def database(root, name):
    p = contained_regular_file(root, name)
    for suffix in ('-wal', '-journal', '-shm'):
        s = Path(str(p)+suffix)
        if s.exists() or s.is_symlink():
            need(not s.is_symlink() and s.is_file(), 'Unsafe SQLite sidecar')
            if suffix != '-shm': need(s.stat().st_size == 0, 'Audit needs stopped/checkpointed SQLite')
    return p



def validate_phase_order(phases, branch_order):
    """Only a prefix of the frozen phase sequence may name evidence folders."""
    need(isinstance(phases, list), 'Invalid phases')
    need(all(isinstance(row, dict) and isinstance(row.get('phase'), str) for row in phases), 'Invalid phase row')
    names = [row['phase'] for row in phases]
    expected = ['capture'] + list(branch_order)
    need(bool(names) and len(names) <= len(expected) and names == expected[:len(names)], 'Bad phase order')
    return names


def check_search_receipt(args, value, blobs, search_mode, max_chars=6000):
    """Check returned spans, not exhaustive recall over later archive contents.

    A final archive can contain objects created after this search. Therefore this
    checker validates every returned item without claiming result-set completeness.
    """
    query=args.get('query'); chars=args.get('chars',min(2000,max_chars))
    need(isinstance(query,str) and 1 <= len(query) <= 256, 'Invalid search query')
    need(type(chars) is int and 1 <= chars <= max_chars, 'Invalid search quota')
    need(isinstance(value,dict) and isinstance(value.get('matches'),list), 'Missing search matches')
    rows=value['matches']; need(len(rows)<=5,'Too many search matches')
    ids=[r.get('handle') for r in rows if isinstance(r,dict)]
    need(len(ids)==len(rows) and all(isinstance(h,str) for h in ids) and ids==sorted(set(ids)), 'Unsorted or repeated search handles')
    span=min(200,chars//max(1,len(rows)))
    # SQLite lower() folds ASCII only; Python str.lower() is not equivalent.
    translation=str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')
    for r in rows:
        h=r['handle'];need(h in blobs,'Unknown search handle');text=blobs[h]
        offset=text.translate(translation).find(query.translate(translation))
        need(offset>=0,'Search match does not contain the literal query')
        if search_mode=='head':
            expected={'handle':h,'excerpt':text[:span]}
        else:
            lo=max(0,offset-max(0,span-len(query))//2);hi=min(len(text),lo+span)
            expected={'handle':h,'match_start':offset,'match_end':offset+len(query),'start':lo,'end':hi,
                      'total_chars':len(text),'excerpt':text[lo:hi]}
        need(r==expected,'Search excerpt or interval mismatch')
    expected_meta={} if search_mode=='head' else {'offset_unit':'unicode_codepoints','matching':'literal_sqlite_ascii_case_insensitive','search_mode':'match'}
    need({k:v for k,v in value.items() if k!='matches'}==expected_meta,'Search metadata mismatch')
    return len(rows)


def audit(run: Path):
    need(not run.is_symlink(), 'Run directory must not be symlink')
    run = run.resolve(strict=True)
    plan_dict = load(run, 'PLAN.json'); plan = SourcePlan.model_validate(plan_dict)
    summary = load(run, 'summary.json')
    need(summary['plan_sha'] == digest(plan_dict) and summary['source_sha256'] == plan.source_sha256, 'Plan/source identity mismatch')
    need(summary['quality_or_superiority_established'] is False and summary['paired_effect_identified'] is False,
         'An interface run is not a method superiority or causal population certificate')
    need(summary['read_only_replay_not_os_snapshot'] is True, 'Snapshot scope changed')
    if plan.provider.mock:
        need(summary['real_model_calls'] == 0 and summary['scripted_actor_only'] is True, 'Mock run mislabelled')
    need(summary['source_unchanged'] and summary['fixture_unchanged'], 'Run recorded input drift')
    task=load_task(run/'TASK.json')
    need(task['task_sha256']==plan.task_sha256 and task['snapshot_sha256']==plan.snapshot_sha256,'Task plan mismatch')
    executor=load(run,'EXECUTOR.json')
    need(digest(executor['core'])==plan.source_sha256 and digest(executor['research'])==plan.research_sha256,'Executor manifest mismatch')
    identities=task['files'];actual=tree_manifest(run/'workspace')
    need(actual==identities,'Public source bytes changed')
    need(load(run,'PUBLIC.json')=={'task_sha256':task['task_sha256'],'snapshot_sha256':task['snapshot_sha256'],'prompt':render_prompt(task),'gold_in_actor_workspace':False},'Public descriptor changed')
    tape = unseal_cache(contained_regular_file(run, 'tape.json').read_text())['entries']
    boundary = unseal_cache(contained_regular_file(run, 'boundary.json').read_text()) if (run/'boundary.json').exists() else None
    phases = summary['phases']; names = validate_phase_order(phases, plan.branch_order)
    if summary['status'] == 'paired_complete':
        need(names == ['capture'] + plan.branch_order and boundary is not None, 'Missing paired arms')
    elif summary['status'] == 'no_eligible_prefix':
        need(names == ['capture'] and boundary is None and phases[0]['state'] == 'completed', 'False no-eligible label')
    else:
        need(summary['status'] == 'stopped', 'Invalid terminal run status')
    ledger_path = database(run, 'accounting.sqlite')
    with readonly_db(ledger_path, immutable_snapshot=True) as db:
        need(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'Accounting database corrupt')
        meta = {r['key']: strict_json_loads(r['value']) for r in db.execute('select * from meta')}
        need(meta['paired_plan'] == plan_dict and meta['budget'] == plan.budget.model_dump(), 'Ledger not bound to plan')
        attempts = [dict(r) for r in db.execute('select * from attempts order by id')]
        ops = {r['op']: dict(r) for r in db.execute('select * from operations')}
    attempt_ops = [r['op'] for r in attempts]
    need(len(attempt_ops) == len(set(attempt_ops)), 'Single-attempt plan retried an operation')
    sent_ops = []
    first_source = {}; first_wire = {}; output = []; completed_raw = {}
    for phase_row in phases:
        phase = phase_row['phase']; folder = run/phase
        need(load(folder, 'phase.json') == phase_row and phase_row['session_revoked'] and phase_row['fixture_unchanged'], 'Phase/lifecycle mismatch')
        need(phase_row['manual_compact_rpc'] is False and phase_row['tools'] == paired_tools(plan), 'Changed experiment tool or intervention scope')
        st = load(folder, 'agent-config/settings.json')
        need(st['compaction'] == {'enabled': True, 'keepRecentTokens': 20000, 'reserveTokens': 66560}, 'Inherited native settings changed')
        events = load(folder, 'events.json')
        need([e['index'] for e in events] == list(range(len(events))), 'Missing/duplicate event')
        if events:
            user=[m['content'] for m in events[0]['source']['messages'] if m.get('role')=='user']
            need(user==[render_prompt(task)],'Actual initial task prompt differs')
        raw_results, raw_calls = transcript(folder)
        need(all(c['name'] != 'read' or c['arguments'].get('path') in task['files'] for c in raw_calls.values()),'Read escaped public source allowlist')
        rpc_rows = [strict_json_loads(line) for line in contained_regular_file(folder, 'rpc.jsonl').read_text().splitlines() if line.strip()]
        need(not any(e.get('command') == 'compact' for e in rpc_rows), 'Manual compact was issued')
        with readonly_db(database(folder, 'gateway/archive.sqlite'), immutable_snapshot=True) as db:
            blobs = {r['handle']: r['content'] for r in db.execute("select * from blobs where namespace='paired'")}
            for h,content in blobs.items(): need(digest(content) == h, 'Archive content hash invalid')
        with readonly_db(database(folder, 'gateway/projection.sqlite'), immutable_snapshot=True) as db:
            projection = [dict(r) for r in db.execute('select * from projection_events order by seq')]
            observations = {r['call_id']: dict(r) for r in db.execute("select * from observations where namespace='paired'")}
        need(len(events) == len(projection), 'Gateway/backend trace count differs')
        with readonly_db(database(folder, 'gateway/sessions.sqlite'), immutable_snapshot=True) as db:
            ss = [dict(r) for r in db.execute('select * from sessions')]
            need(len(ss) == 1 and ss[0]['id'] == 'paired' and ss[0]['disabled'] == 1 and ss[0]['revision'] == 0,
                 'Session not fresh/revoked or full-history compaction was used')
            need(db.execute('select count(*) from compact_ops').fetchone()[0] == 0, 'Unexpected full-history compaction')
        with readonly_db(database(folder, 'gateway/ledger.sqlite'), immutable_snapshot=True) as db:
            need(db.execute('select count(*) from attempts').fetchone()[0] == 0, 'Unaccounted dispatch on per-gateway ledger')
        wire_rows = load(folder, 'wire/index.json') if (folder/'wire/index.json').exists() else []
        full = {}; packed = set(); replay_count = 0; phase_dispatched = 0; first_index = len(tape)
        for e,pj in zip(events,projection):
            rec = unseal_cache(pj['record'])
            need(rec == e['prepared'] and pj['op'] == e['gateway_op'], 'Projection record mismatch')
            expect_cfg = plan.policy.model_copy(update={'mode':'apply' if phase == 'projected' else 'observe'}).model_dump()
            need({'recovery_interface':'legacy', **rec['config']} == expect_cfg, 'Frozen policy changed')
            normalized = [asdict(Message.from_dict(m)) for m in e['source']['messages']]
            need(rec['source_messages'] == normalized and e['source_sha'] == digest(e['source']), 'Input record mismatch')
            # Reference implementation evaluates decisions from ORIGINAL Pi results,
            # not the claimed eligible/applied counters. Failed prepared ops do not
            # count as full exposures.
            new_full, new_packed = dict(full), set(packed)
            check_record(rec, new_full, new_packed, raw_results, blobs)
            if pj['state'] == 'complete': full, packed = new_full, new_packed
            else: need(e['state'] in {'boundary','failed'}, 'Unexpected unresolved projection')
            if e['route'] == 'prefix_replay':
                need(phase != 'capture' and e['index'] < len(tape), 'Replay outside common prefix')
                t = tape[e['index']]
                need(e['source'] == t['source'] and rec['sent_messages'] == t['sent'] and e['response'] == t['response'], 'Replay differs from completed tape')
                need(rec['applied_count'] == 0 and e['state'] == 'complete', 'Prefix was modified or incomplete')
                need('central_op' not in e and 'wire_index' not in e, 'Replay claimed new upstream work')
                replay_count += 1
            elif e['route'] == 'paused_before_claim':
                need(phase == 'capture' and e['state'] == 'boundary' and rec['eligible_count'] > 0 and rec['applied_count'] == 0,
                     'Invalid boundary pause')
                need(e['index'] == len(tape) and boundary['source'] == e['source'] and boundary['prepared'] == rec,
                     'Boundary not earliest qualified prefix')
            elif e['route'] == 'central_dispatch':
                op = e['central_op']; need(op not in sent_ops, 'Duplicate logical dispatch')
                sent_ops.append(op)
                need(op in ops and ops[op]['cell'] == phase, 'Missing central operation')
                need(op == f'rc7:{phase}:{phase_dispatched:03d}', 'Dispatch identity sequence changed')
                phase_dispatched += 1
                if e['state'] != 'complete': continue  # Unknown/failed cost remains; cannot be a complete paired run.
                idx = e['wire_index']; need(type(idx) is int and 0 <= idx < len(wire_rows), 'Missing raw wire record')
                w = wire_rows[idx]
                req = contained_regular_file(folder/'wire', w['request_file']).read_bytes()
                resp = contained_regular_file(folder/'wire', w['response_file']).read_bytes()
                need(hashlib.sha256(req).hexdigest() == w['request_sha256'] and hashlib.sha256(resp).hexdigest() == w['response_sha256'], 'Raw wire hash mismatch')
                need(len(resp) == w['response_bytes'] and w.get('body_complete') is True and w['status'] == 200, 'Incomplete/non-200 wire is not a completed reply')
                path, expected = build_request(plan.provider, [Message.from_dict(m) for m in rec['sent_messages']],
                                              e['source']['tools'], effort=e['source']['effort'], max_output_tokens=e['source']['max_output_tokens'])
                need(strict_json_loads(req) == expected and w['path'].endswith(path), 'Raw request differs from projected canonical request')
                decoded = decode_recorded_body(resp, w.get('content_encoding',''), plan.provider.max_response_bytes)
                actual_completion = parse_completion(plan.provider, strict_json_loads(decoded)).to_dict()
                need(actual_completion == e['response'] == unseal_cache(ops[op]['result']), 'Response/tape/ledger mismatch')
                raw_usage = actual_completion['usage']; a = next(x for x in attempts if x['op'] == op)
                tokens, _ = normalize_usage(raw_usage, plan.provider.prices)
                need(a['actual_tokens'] == tokens and strict_json_loads(a['raw_usage']) == raw_usage, 'Usage differs from raw response')
                need(ops[op]['payload_sha'] == digest({'profile':digest(plan.provider.model_dump()),'path':path,'body':expected}), 'Central payload hash mismatch')
                completed_raw[op] = e['response']
                if phase == 'capture':
                    need(rec['eligible_count'] == 0, 'Capture continued past first eligible state')
                    t = tape[e['index']]
                    need(t == {'source':e['source'], 'sent':rec['sent_messages'], 'response':e['response'], 'central_op':op}, 'Tape not the raw completed prefix')
                elif e['index'] == first_index:
                    need(e['source'] == boundary['source'] and rec['eligible_count'] > 0, 'Branches did not start at the same eligible state')
                    need((rec['applied_count'] > 0) == (phase == 'projected'), 'Treatment not activated in projected arm')
                    first_source[phase] = e['source']; first_wire[phase] = expected
            else:
                need(e['state'] == 'failed', 'Unsupported event route')
        for call_id, val in observations.items():
            need(val['full_sends'] == full.get(call_id,0) and bool(val['packed']) == (call_id in packed), 'Stored exposure counters disagree')
        need(set(observations) == set(full)|packed, 'Exposure inventory mismatch')
        need(replay_count == phase_row['prefix_replays'], 'Replay count mismatch')
        if phase != 'capture' and phase_row['state'] == 'completed': need(replay_count == len(tape), 'Prefix not fully replayed')
        if phase_row['state'] == 'completed':
            assistants = [x.get('message') for x in rpc_rows if x.get('type') == 'message_end' and x.get('message',{}).get('role') == 'assistant']
            need(assistants and assistants[-1]['stopReason'] == 'stop', 'Missing final native assistant answer')
            answer = ''.join(x.get('text','') for x in assistants[-1]['content'] if x.get('type') == 'text')
            need(answer == phase_row['answer'] and phase_row['answer_scored'] is False, 'Answer transcript changed or scorer leaked into actor')
        exact = rejected = 0
        study = load(folder, 'study.json')
        for ident, call in raw_calls.items():
            args=call['arguments']
            if call['name'] in {'recover_evidence','search_evidence'} and 'query' in args and 'handle' not in args:
                result=raw_results[ident]
                need(not result['is_error'], 'Search was not a successful receipt')
                check_search_receipt(args, strict_json_loads(result['text']), blobs,
                                     study['compression'].get('recovery_search_mode','head'), study['compression']['recovery_chars'])
            if call['name'] == 'recover_evidence' and 'handle' in call['arguments']:
                args = call['arguments']; raw = raw_results[ident]['text']
                if args.get('handle') is not None and args.get('query') is not None:
                    need(raw == 'ReVer halted: recovery_arguments', 'Mixed recovery arguments were not rejected')
                    rejected += 1
                    continue
                val = strict_json_loads(raw)
                content = blobs.get(args['handle']); need(content is not None, 'Recovery handle absent')
                start = args.get('start',0); default_chars = min(2000,study['compression']['recovery_chars']) if plan.policy.recovery_interface == 'split_v1' else 2000
                chars = args.get('chars',default_chars)
                need(val.get('text') == content[start:start+chars], 'Recovery not an exact archive interval')
                exact += 1
        output.append({'phase':phase,'status':phase_row['state'],'dispatch_intents':phase_dispatched,'actual_attempts':sum(a['cell']==phase for a in attempts),
                       'prefix_replays':replay_count,'exact_recovery_calls':exact,'rejected_recovery_calls':rejected,
                       'applied_requests':sum(x['prepared']['applied_count'] > 0 for x in events),
                       'answer_scored':False})
    need(set(ops) == set(sent_ops), 'Unrecorded operations in shared ledger')
    first_pair = set(first_source) == {'full','projected'}
    if first_pair:
        need(first_source['full'] == first_source['projected'], 'First branch source mismatch')
        # check_record already proves that only selected tool-result content changed.
        need(first_wire['full'] != first_wire['projected'], 'No actual request representation change')
        for k in set(first_wire['full']) | set(first_wire['projected']):
            if k not in {'messages','input'}:
                need(first_wire['full'].get(k) == first_wire['projected'].get(k), 'Non-message provider setting changed')
    elif summary['status'] == 'paired_complete':
        need(False, 'Missing first paired request')
    cost = audit_ledger(ledger_path, immutable_snapshot=True)
    need(cost['totals']['attempts'] == summary['central_cost']['attempts'] and cost['totals']['observed_tokens'] == summary['central_cost']['known_tokens']
         and cost['totals']['accounted_tokens'] == summary['central_cost']['accounted_tokens'], 'Summary cost mismatch')
    return {'schema':1,'kind':'reverpi.s4.readonly-source-audit','status':'passed','run_status':summary['status'],
            'plan_sha':digest(plan_dict),'source_sha256':plan.source_sha256,'phases':output,
            'shared_prefix_completed_calls':len(tape),'first_pair_identical_except_projection':first_pair,
            'experiment_new_call_count':cost['totals']['attempts'], 'experiment_observed_tokens':cost['totals']['observed_tokens'],
            'unknown_attempts':cost['totals']['unknown_attempts'],'model_quality_verified':False,
            'general_agent_superiority':False,'mock':plan.provider.mock,
            'cost_scope':'prefix charged once in experiment, full prefix counts toward each arm deployment cost'}

