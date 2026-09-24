#!/usr/bin/env python3
"""Audit a STATIC RC5 delivery on disposable database-family copies.

Every consumed database/sidecar must be covered by the input manifest. This does
not synchronize an actively modified tree or grant any model/execution authority.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from reverpi.util import (contained_regular_file, strict_json_loads, unseal_cache,
                          bytes_digest, digest, atomic_create)

SUFFIXES = ('', '-wal', '-shm', '-journal')


def sha(path: Path) -> str:
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def family_paths(source: Path) -> list[Path]:
    return [Path(str(source) + s) for s in SUFFIXES
            if Path(str(source) + s).exists() or Path(str(source) + s).is_symlink()]


def copy_sqlite_family(source: Path, target: Path, *, root: Path | None = None,
                       manifest: dict | None = None) -> dict:
    base = (root if root is not None else source.parent).resolve(strict=True)
    before = {}
    parts = family_paths(source)
    if source not in parts:
        raise ValueError('Missing database')
    # Validate the entire family BEFORE copying any component.
    for part in parts:
        try:
            rel = part.relative_to(base).as_posix()
        except ValueError:
            raise ValueError('Database outside evidence root') from None
        safe = contained_regular_file(base, rel)
        value = sha(safe)
        if manifest is not None and manifest.get(rel) != value:
            raise ValueError('Database component absent from manifest or changed')
        before[rel] = value
    for part in parts:
        rel = part.relative_to(base).as_posix()
        suffix = str(part)[len(str(source)):]
        destination = Path(str(target) + suffix)
        with contained_regular_file(base, rel).open('rb') as src, destination.open('xb') as dst:
            shutil.copyfileobj(src, dst)
        if sha(destination) != before[rel]:
            raise ValueError('Database changed during static copy')
    after = {p.relative_to(base).as_posix(): sha(contained_regular_file(base, p.relative_to(base).as_posix()))
             for p in family_paths(source)}
    if before != after:
        raise ValueError('Database family changed during static copy')
    return before


def verify_manifest(root: Path, manifest: dict, manifest_sha: str) -> None:
    if sha(contained_regular_file(root, 'MANIFEST_SHA256.json')) != manifest_sha:
        raise ValueError('Input manifest changed during audit')
    for rel, expected in manifest.items():
        if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
            raise ValueError('Invalid manifest digest')
        if sha(contained_regular_file(root, rel)) != expected:
            raise ValueError('Supplied manifest failed verification')


def review_shapes(con: sqlite3.Connection, plan: dict | None) -> list[dict]:
    shapes = []
    for row in con.execute("SELECT op,result FROM operations WHERE substr(op,1,6)='scope_' AND result IS NOT NULL ORDER BY created,op"):
        shape = {'op': row['op'], 'cached_envelope_valid': False,
                 'original_strict_validation_passed': None,
                 'posthoc_unwrapping_used_for_official_score': False}
        try:
            response = unseal_cache(row['result'])
            shape['cached_envelope_valid'] = True
            parsed = strict_json_loads(response['text'])
            shape['top_level_keys'] = sorted(parsed) if isinstance(parsed, dict) else None
            usage = response.get('usage') or {}
            details = usage.get('output_tokens_details') or {}
            shape.update(cache_schema=1, total_tokens=usage.get('total_tokens'),
                         output_tokens=usage.get('output_tokens'), reasoning_tokens=details.get('reasoning_tokens'))
            if plan is not None:
                for job in plan.get('jobs', []):
                    if row['op'] == 'scope_' + digest({'plan': digest(plan), 'job': job['index']})[:32]:
                        from reverpi.lean_review import validate_review
                        try:
                            validate_review(response['text'], job)
                            shape['original_strict_validation_passed'] = True
                        except ValueError:
                            shape['original_strict_validation_passed'] = False
                        shape['validation_basis'] = 'cached_text_and_frozen_job_no_model_call'
                        break
        except Exception as exc:
            # An audit cannot repair an invalid cache. Preserve an explicit diagnostic.
            shape['shape_error'] = type(exc).__name__
        shapes.append(shape)
    return shapes


def audit(root: Path) -> dict:
    root = root.resolve(strict=True)
    mf = contained_regular_file(root, 'MANIFEST_SHA256.json')
    raw = mf.read_bytes(); envelope = strict_json_loads(raw)
    manifest = envelope.get('files') if isinstance(envelope, dict) else None
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError('Input must contain a nonempty files manifest')
    manifest_sha = bytes_digest(raw)
    verify_manifest(root, manifest, manifest_sha)
    paths = [('campaign/review','flash-rc5-one-campaign/review',False),
             ('campaign/dev-01','flash-rc5-one-campaign/dev-01',False),
             ('campaign/dev-02','flash-rc5-one-campaign/dev-02',False),
             ('out_of_campaign/live_diagnostic','diagnostic-dev02-rootcause-matrix',False),
             ('out_of_campaign/mock_diagnostic','diagnostic-dev02-mock-protocol',True)]
    ledgers, shapes, families = [], [], {}
    for label, rel, mock in paths:
        source = contained_regular_file(root, rel + '/ledger.sqlite')
        with tempfile.TemporaryDirectory(prefix='reverpi-static-audit-') as tmp:
            target = Path(tmp)/'ledger.sqlite'
            components = copy_sqlite_family(source, target, root=root, manifest=manifest)
            families[rel] = components
            con = sqlite3.connect(target); con.row_factory = sqlite3.Row
            try:
                check = con.execute('PRAGMA integrity_check').fetchone()[0]
                if check != 'ok':
                    raise ValueError('Database integrity check failed')
                cost = dict(con.execute('SELECT COUNT(*) attempts, COALESCE(SUM(actual_tokens),0) observed_tokens, '
                    'COALESCE(SUM(CASE WHEN actual_tokens IS NULL THEN reserve_tokens ELSE 0 END),0) '
                    'unknown_reserved_tokens FROM attempts').fetchone())
                states = {x[0]: x[1] for x in con.execute('SELECT state,COUNT(*) FROM operations GROUP BY state')}
                ledgers.append({'scope':label, 'path':rel, 'mock':mock, **cost,
                    'operation_states':states, 'accounted_tokens':cost['observed_tokens']+cost['unknown_reserved_tokens'],
                    'currency_cost':None, 'database_sha256':components[rel+'/ledger.sqlite'],
                    'component_sha256':components, 'integrity_check':check})
                if label == 'campaign/review':
                    plan_rel = rel + '/plan.json'
                    plan = strict_json_loads(contained_regular_file(root, plan_rel).read_bytes()) if plan_rel in manifest else None
                    shapes = review_shapes(con, plan)
            finally:
                con.close()
    # Never use assert for integrity acceptance: this also runs under python -O.
    verify_manifest(root, manifest, manifest_sha)
    for rel, before in families.items():
        after = {p.relative_to(root).as_posix(): sha(contained_regular_file(root, p.relative_to(root).as_posix()))
                 for p in family_paths(root / rel / 'ledger.sqlite')}
        if before != after:
            raise ValueError('Database family set changed during audit')
    campaign = [x for x in ledgers if x['scope'].startswith('campaign/')]
    paid = [x for x in ledgers if not x['mock']]
    return {'schema':2, 'input_manifest_files_checked':len(manifest), 'input_unchanged':True,
            'ledgers':ledgers, 'campaign_observed_tokens':sum(x['observed_tokens'] for x in campaign),
            'all_live_observed_tokens':sum(x['observed_tokens'] for x in paid),
            'all_live_unknown_reserved_tokens':sum(x['unknown_reserved_tokens'] for x in paid),
            'scope_review_failure_shape':shapes[0] if len(shapes)==1 else None,
            'scope_review_failure_shapes':shapes,
            'official_24_task_plan_file_found':bool(list(root.rglob('flash-paired-24.json'))),
            'plan_filename_is_not_official_benchmark_certification':True,
            'paid_diagnostic_authorization_found_in_campaign':False,
            'historical_missing_environment_proven':False,
            'unlisted_files_verified':False, 'concurrent_filesystem_snapshot_certified':False,
            'note':'Static archive only. Local credentials history and any authorization outside this archive remain unknown.'}


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--delivery',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();result=audit(a.delivery)
    atomic_create(a.out, json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0


if __name__=='__main__':raise SystemExit(main())
