"""Private matrix evidence export, including incomplete runs, with no paid actions.

Requires the complete input/ledger tree, NOT every trajectory to be successful.
A cooperating writer lock plus a private copy and end-to-end rehash protects
against accidental drift. This is not a hostile-writer filesystem snapshot.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile
from .util import (canonical, process_lock, strict_json_loads, digest,
                   safe_id, contained_regular_file, bytes_digest)

MAX_EXPORT_BYTES = 1024 ** 3
MAX_EXPORT_FILES = 30000
SQLITE_HEADER = b'SQLite format 3\x00'
SIDECARS = ('-wal', '-shm', '-journal')


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    result = {}; size = 0
    for path in sorted(root.rglob('*')):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError('Evidence tree contains a symlink or special file')
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        size += path.stat().st_size
        if len(result) >= MAX_EXPORT_FILES or size > MAX_EXPORT_BYTES:
            raise ValueError('Evidence export exceeds its offline size limit')
        result[relative] = file_digest(path)
    return result


def export_run(root: Path, out: Path) -> dict:
    if root.is_symlink() or out.is_symlink():
        raise ValueError('Symlinks are not admitted for run exports')
    root = root.resolve(strict=True); out = out.resolve()
    if out.is_relative_to(root) or out.exists():
        raise ValueError('Use a new export path outside the source run')
    lock = root / 'writer.lock'
    if lock.exists() or lock.is_symlink():
        contained_regular_file(root, 'writer.lock')
    from .intervention_matrix import tree_identity, read_committed
    from .paired_interventions import read_intervention
    from .revalidation import load_registry
    with process_lock(lock), tempfile.TemporaryDirectory(prefix='reverpi-export-') as temp:
        original = inventory(root)  # Before ANY plan, parent, template or COMMIT read.
        required = ('plan.json', 'parent.json', 'registry.json', 'ledger.sqlite')
        if any(name not in original for name in required) or not (root / 'template').is_dir():
            raise ValueError('Export requires the full input/ledger tree, not only result rows')
        if 'EXPORT_MANIFEST.json' in original:
            raise ValueError('Do not re-export an unpacked export without checking its original manifest')
        plan = strict_json_loads(contained_regular_file(root, 'plan.json').read_bytes())
        if plan.get('schema') != 1 or plan.get('kind') != 'development_same_parent_matrix':
            raise ValueError('This exporter supports same-parent development matrices')
        read_intervention(root / 'parent.json', plan['parent_sha256'])
        load_registry(root / 'registry.json', plan['registry_sha256'])
        if tree_identity(root / 'template') != plan['workspace_files']:
            raise ValueError('Workspace template changed')
        states = {}; seen = set()
        for unit in plan['units']:
            name = safe_id(unit['unit'])
            if name in seen:
                raise ValueError('Duplicate planned unit')
            seen.add(name)
            folder = root / 'arms' / name
            if (folder / 'COMMIT.json').exists():
                read_committed(folder, unit, digest(plan))
                states[name] = 'committed'
            else:
                states[name] = 'uncommitted' if folder.exists() else 'not_started'
        snapshot = Path(temp) / 'snapshot'; snapshot.mkdir()
        for relative in original:
            target = snapshot / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(contained_regular_file(root, relative), target)
            if file_digest(target) != original[relative]:
                raise ValueError('Evidence changed while copying')
        # Detect all SQLite payloads by header, not filename extension alone.
        databases = []
        for relative in original:
            with (snapshot / relative).open('rb') as stream:
                header = stream.read(16)
            if header == SQLITE_HEADER and not relative.startswith('template/'):
                databases.append(relative)
        if 'ledger.sqlite' not in databases:
            raise ValueError('The required ledger is not a SQLite database')
        db_backups = {}
        for index, relative in enumerate(databases):
            copied = snapshot / relative
            backup = Path(temp) / f'backup-{index}.sqlite'
            # Recover WAL/rollback journals ONLY in the private copy, never source.
            db = sqlite3.connect(copied, timeout=5); target = sqlite3.connect(backup)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('SQLite integrity check failed')
                db.backup(target)
            finally:
                target.close(); db.close()
            os.replace(backup, copied)
            db_backups[relative] = 'recovered_private_copy_then_sqlite_backup'
        consumed_sidecars = {name + suffix for name in databases for suffix in SIDECARS}
        # Preserve every frozen template byte, including caches referenced by its identity.
        names = [name for name in original if name.startswith('template/') or
                 (name not in consumed_sidecars and not name.endswith('.lock'))]
        for required_name in required:
            if required_name not in names:
                raise ValueError('Required evidence missing from final payload')
        if sum((snapshot / n).stat().st_size for n in names) > MAX_EXPORT_BYTES:
            raise ValueError('SQLite backup expansion exceeds export size limit')
        manifest = {name: file_digest(snapshot / name) for name in names}
        if tree_identity(snapshot / 'template') != plan['workspace_files']:
            raise ValueError('Export changed the frozen workspace template')
        if digest(strict_json_loads((snapshot / 'plan.json').read_bytes())) != digest(plan):
            raise ValueError('Exported plan identity changed')
        if inventory(root) != original:
            raise ValueError('Source changed during export')
        metadata = {'schema':2, 'plan_sha256':digest(plan), 'files':manifest,
                    'unit_states':states, 'all_planned_units_committed':all(v=='committed' for v in states.values()),
                    'sqlite_consistent_backup':True, 'sqlite_backup_methods':db_backups,
                    'sqlite_backup_scope':'runtime databases outside frozen template',
                    'frozen_template_copied_byte_for_byte':True,
                    'regular_file_copy_and_final_source_rehash':True,
                    'filesystem_atomic_snapshot_certified':False,
                    'paid_calls':0, 'provider_inflight_requests_not_reconciled':True,
                    'public_release_redaction_done':False, 'distribution_scope':'private_evidence_handoff'}
        out.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.pending-export-', dir=out.parent)
        os.close(fd)
        try:
            with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as z:
                for relative in names:
                    z.write(snapshot / relative, 'matrix/' + relative)
                z.writestr('matrix/EXPORT_MANIFEST.json', canonical(metadata))
            # Publication occurs only after a second source check, not over an old artifact.
            if inventory(root) != original:
                raise ValueError('Source changed while publishing export')
            with open(temporary, 'rb') as stream:
                os.fsync(stream.fileno())
            os.link(temporary, out, follow_symlinks=False)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return {'files':len(manifest)+1, 'zip_sha256':file_digest(out), 'path':str(out),
            'paid_calls':0, 'public_release_redaction_done':False,
            'all_planned_units_committed':metadata['all_planned_units_committed']}
