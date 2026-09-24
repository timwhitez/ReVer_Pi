#!/usr/bin/env python3
"""Verify a static TB24 supplement without model calls, task execution or remote reads.

Stored absolute server paths are identifiers, never opened. All SQLite reads use
private copies after validating the manifest and all present SQLite sidecars.
Native compaction evidence is NOT inferred from gateway revision counters.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from reverpi.util import atomic_create, canonical, contained_regular_file, digest, strict_json_loads
from audit_official_window import exact_sign, hash_file, ledger_data, validate_rows
from audit_rc5_campaign import copy_sqlite_family


def validate_manifest(root: Path) -> dict[str, dict]:
    rows = strict_json_loads(contained_regular_file(root, 'SUPPLEMENT_MANIFEST.json').read_bytes())
    if not isinstance(rows, list) or not rows:
        raise ValueError('Nonempty supplement manifest required')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            raise ValueError('Malformed manifest row')
        name = row['path']
        if name in result or type(row.get('bytes')) is not int or row['bytes'] < 0:
            raise ValueError('Duplicate manifest path or invalid size')
        p = contained_regular_file(root, name)
        if p.stat().st_size != row['bytes'] or hash_file(p) != row.get('sha256'):
            raise ValueError('Supplement manifest mismatch: ' + name)
        result[name] = row
    # Never silently ignore an unmanifested hot journal/WAL.
    for name in result:
        if name.endswith(('.sqlite', '.db')):
            for suffix in ('-wal', '-shm', '-journal'):
                p = root / (name + suffix)
                if (p.exists() or p.is_symlink()) and name + suffix not in result:
                    raise ValueError('Unmanifested SQLite sidecar')
    return result


def read_copy(source: Path, target: Path, root: Path, table: str) -> list[dict]:
    if table not in {'sessions', 'compact_ops', 'blobs', 'recovery'}:
        raise ValueError('Unknown audited table')
    copy_sqlite_family(source, target, root=root)
    with sqlite3.connect(target) as con:
        con.row_factory = sqlite3.Row
        if con.execute('pragma integrity_check').fetchone()[0] != 'ok':
            raise ValueError('SQLite integrity error')
        return [dict(r) for r in con.execute('select * from ' + table)]


def official_check(row: dict, official: dict) -> None:
    kwargs = official.get('config', {}).get('agent', {}).get('kwargs', {})
    if kwargs.get('cell_id') != row['cell'] or kwargs.get('method') != row['method']:
        raise ValueError('Official result cell/method mismatch')
    reward = official.get('verifier_result', {}).get('rewards', {}).get(row['reward_key'])
    if reward != row['official_reward'] or official.get('task_checksum') != row['official_task_checksum']:
        raise ValueError('Official result reward/checksum mismatch')


def audit(root: Path) -> dict:
    root = root.resolve(strict=True)
    manifest = validate_manifest(root)
    def path(name):
        if name not in manifest:
            raise ValueError('Required artifact is not in supplement manifest: ' + name)
        return contained_regular_file(root, name)
    def read(name):
        return strict_json_loads(path(name).read_bytes())
    plan = read('tb24-official-flash.json')
    rows = read('results.json')
    actual = validate_rows(rows, plan['grid'])
    official_by_sha = defaultdict(list)
    for name, entry in manifest.items():
        if name.startswith('official-results/') and name.endswith('_result.json'):
            official_by_sha[entry['sha256']].append(name)
    if len(official_by_sha) != len(rows):
        raise ValueError('Official result coverage mismatch')
    for row in rows:
        names = official_by_sha.get(row['official_result_sha'], [])
        if len(names) != 1:
            raise ValueError('Missing or ambiguous original official result')
        official_check(row, read(names[0]))
    with tempfile.TemporaryDirectory(prefix='rever-supp-audit-') as temp:
        t = Path(temp)
        sessions = read_copy(path('sqlite-backup/sessions.backup.sqlite'), t/'sessions.sqlite', root, 'sessions')
        compact = read_copy(path('sqlite-backup/sessions.backup.sqlite'), t/'compact.sqlite', root, 'compact_ops')
        blobs = read_copy(path('sqlite-backup/archive.backup.sqlite'), t/'blobs.sqlite', root, 'blobs')
        recovery = read_copy(path('sqlite-backup/archive.backup.sqlite'), t/'recovery.sqlite', root, 'recovery')
        cost, attempts, _ = ledger_data(path('sqlite-backup/ledger.backup.sqlite'), t/'ledger.sqlite', root)
        review_cost, _, _ = ledger_data(path('review-frozen-plan/ledger.sqlite'), t/'review.sqlite', root)
        with sqlite3.connect(t/'ledger.sqlite') as db:
            meta = {k: strict_json_loads(v) for k, v in db.execute('select key,value from meta')}
    by_cell = {r['id']: r for r in sessions}
    if len(by_cell) != len(sessions) or set(by_cell) != set(actual):
        raise ValueError('Gateway sessions do not cover the exact result grid')
    if any(a['cell'] not in actual for a in attempts) or any(c['session'] not in actual for c in compact):
        raise ValueError('Unknown ledger or compaction session')
    profiles = [v for k, v in meta.items() if k.startswith('provider:')]
    if len(profiles) != 1:
        raise ValueError('Ambiguous provider snapshot')
    provider = profiles[0]
    # This matches the frozen launcher formula; not a recovered on-disk settings file.
    reserve = max(provider['max_output_tokens'] + 1024, min(16384, provider['context_window']//4))
    if not 0 < reserve < provider['context_window']:
        raise ValueError('Invalid frozen context/output reserve')
    settings = {'enabled': True, 'reserveTokens': reserve,
                'keepRecentTokens': min(20000, max(256, (provider['context_window']-reserve)//2))}
    declared_missing = read('missing_sessions.json')
    if len({x['cell'] for x in declared_missing}) != len(declared_missing):
        raise ValueError('Duplicate missing-session entry')
    cell_rows = []
    native_missing = []
    seen_logs = set()
    for row in rows:
        state = by_cell[row['cell']]
        if state['method'] != row['method'] or type(state['revision']) is not int or state['revision'] < 0:
            raise ValueError('Gateway method/revision mismatch')
        # Prefix matches must be unique; never treat a collision as evidence.
        names = [name for name in manifest if name.startswith('session-logs/'+row['cell'][:12]+'_')
                 and name.endswith('_session.jsonl')]
        if len(names) > 1:
            raise ValueError('Ambiguous session log prefix')
        native_count = None
        if names:
            seen_logs.add(names[0])
            entries = [strict_json_loads(line) for line in path(names[0]).read_bytes().split(b'\n') if line.strip()]
            if not entries or not isinstance(entries[0], dict) or entries[0].get('type') != 'session':
                raise ValueError('Invalid Pi session header')
            native_count = sum(e.get('type') == 'compaction' for e in entries)
        else:
            native_missing.append(row['cell'])
        aa = [a for a in attempts if a['cell'] == row['cell']]
        known = sum(a['actual_tokens'] for a in aa if a['actual_tokens'] is not None)
        unknown = sum(a['reserve_tokens'] for a in aa if a['actual_tokens'] is None)
        if (row['cost']['known_tokens'] != known or row['cost']['accounted_tokens'] != known+unknown
                or row['cost']['attempts'] != len(aa)):
            raise ValueError('Cell billing mismatch')
        peaks = []
        for a in aa:
            if a['raw_usage']:
                u = strict_json_loads(a['raw_usage'])
                peaks.append(u.get('input_tokens', u.get('prompt_tokens', 0)) + u.get('output_tokens', u.get('completion_tokens', 0)))
        cell_rows.append({'cell': row['cell'], 'task': row['task_id'], 'method': row['method'],
            'official_result_sha256': row['official_result_sha'], 'success': row['success'],
            'execution_success': row['execution_success'], 'gateway_revision': state['revision'],
            'gateway_compact_ops': sum(c['session'] == row['cell'] for c in compact),
            'gateway_memory_present': state['memory'] is not None,
            'native_compaction_entries': native_count,
            'native_compaction_status': 'UNKNOWN_MISSING_LOG' if native_count is None else 'OBSERVED_COMMIT' if native_count else 'NO_COMMIT_IN_AVAILABLE_LOG',
            'known_tokens': known, 'unknown_reserved_tokens': unknown,
            'peak_reported_single_response_tokens': max(peaks, default=None),
            'session_log': names[0] if names else None})
    if set(native_missing) != {x['cell'] for x in declared_missing}:
        raise ValueError('Missing-session declaration differs from artifacts')
    available_names = {n for n in manifest if n.startswith('session-logs/') and n.endswith('_session.jsonl')}
    if available_names != seen_logs:
        raise ValueError('Unmapped session logs')
    metadata = read('task-metadata/time-limit-metadata.json')
    ids = {r['task_id'].removeprefix('tb-') for r in rows}
    if len(metadata) != len(ids) or {m['id'] for m in metadata} != ids:
        raise ValueError('Task metadata coverage mismatch')
    for m in metadata:
        task = tomllib.loads(path('task-metadata/'+m['id']+'_task.toml').read_text())
        if (task['agent']['timeout_sec'] != m['agent_timeout_sec']
                or task['verifier']['timeout_sec'] != m['verifier_timeout_sec']):
            raise ValueError('Task timeout metadata differs from original TOML')
    inventory = read('official-inventory.terminal-bench-2-1.METADATA.v2.json')
    items = inventory['tasks']
    if len({t['id'] for t in items}) != len(items) or not ids <= {t['id'] for t in items}:
        raise ValueError('Inventory/task identity mismatch')
    updated = [{**t, 'historical_exposure_label': t.get('exposure'),
                'exposure': 'observed_search' if t['id'] in ids else 'not_observed_in_this_supplement',
                'blind_test_eligibility_certified': False,
                'timeout_metadata_available': t['id'] in ids} for t in items]
    worker_names = [n for n in manifest if n.startswith('worker-identity/') and n.endswith('.tgz')]
    worker_hashes = [manifest[n]['sha256'] for n in worker_names]
    if len(worker_names) != 1:
        raise ValueError('One original worker binary is required for this supplement audit')
    if any(strict_json_loads(s['metadata'])['worker']['archive_sha256'] != worker_hashes[0] for s in sessions):
        raise ValueError('Worker identity differs from gateway session metadata')
    pairs = defaultdict(dict)
    for r in rows:
        key = (r['task_id'], r['repeat'])
        if r['method'] in pairs[key]: raise ValueError('Duplicate pair member')
        pairs[key][r['method']] = r['success']
    if any(set(p) != {'pi_original','mask'} for p in pairs.values()): raise ValueError('Incomplete pair')
    wins = sum(p['mask'] and not p['pi_original'] for p in pairs.values())
    losses = sum(p['pi_original'] and not p['mask'] for p in pairs.values())
    if validate_manifest(root) != manifest:
        raise ValueError('Supplement changed during audit')
    return {'schema':1, 'kind':'supplement_static_audit', 'new_model_calls':0,
        'manifest_files_verified':len(manifest), 'manifest_sha256':hash_file(root/'SUPPLEMENT_MANIFEST.json'),
        'official_results_verified':len(rows), 'input_unchanged':True,
        'worker_binary_present':True, 'worker_sha256':worker_hashes[0],
        'worker_member':worker_names[0], 'source_sha256':meta['gateway_code'],
        'gateway':{'sessions':len(sessions),'revisions':dict(Counter(s['revision'] for s in sessions)),
                   'compact_ops':len(compact),'archive_blobs':len(blobs),'recoveries':len(recovery)},
        'native_logs':{'present':len(seen_logs),'missing':len(native_missing),
                       'commits_in_available_logs':sum(r['native_compaction_entries'] or 0 for r in cell_rows),
                       'missing_by_method':dict(Counter(actual[c]['method'] for c in native_missing)),
                       'all_48_native_absence_proven':not native_missing and all(r['native_compaction_entries']==0 for r in cell_rows)},
        'frozen_provider_public_fields':{k:provider[k] for k in ('model','protocol','effort','context_window','max_output_tokens')},
        'reconstructed_launcher_settings':settings,'settings_provenance':'formula and frozen provider; per-trial settings file not archived',
        'trigger_threshold_tokens_strictly_greater_than':provider['context_window']-reserve,
        'paired':{'tasks':len(pairs),'candidate_wins':wins,'candidate_losses':losses,**exact_sign(wins,losses)},
        'costs':{'tb':cost,'review':review_cost,'total':{k:cost[k]+review_cost[k] for k in ('observed_tokens','unknown_reserved_tokens','accounted_tokens')}},
        'cells':cell_rows,'inventory_count':len(items),'timeout_metadata_count':len(metadata),
        'inventory_with_corrected_exposure':updated,
        'limits':{'task_solutions_read':False,'task_execution':False,'official_g4_certified':False,
            'causal_compression_effect_established':False,'native_attempt_events_complete':False,
            'per_database_backup_is_not_cross_database_atomicity_proof':True,
            'server_writer_shutdown_independently_verified':False,'long_timeout_predicts_activation':False}}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--supplement',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.out.exists() or a.out.resolve().is_relative_to(a.supplement.resolve()):
        raise ValueError('Use a new report outside the original supplement')
    result=audit(a.supplement);atomic_create(a.out,canonical(result))
    print(canonical({k:result[k] for k in ('manifest_files_verified','official_results_verified','gateway','native_logs','new_model_calls')}))
if __name__=='__main__':main()
