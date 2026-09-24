#!/usr/bin/env python3
"""Verify a distribution using only the standard library (POSIX safe-open path).

The current manifest is required. --allow-legacy is an explicit downgrade for
old releases. Hashes are integrity checks, NOT signatures or a filesystem-wide
atomic snapshot; unlisted files and hostile concurrent writers are not certified.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


def strict_json(raw: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate manifest key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Non-finite manifest value')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid_constant)


def path_parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str):
        raise ValueError('Path must be a string')
    part = PurePosixPath(relative)
    if (not part.parts or part.is_absolute() or str(part) != relative
            or any(x in {'.', '..'} for x in part.parts) or '\\' in relative or ':' in relative):
        raise ValueError('Unsafe or noncanonical path')
    return part.parts


@contextlib.contextmanager
def regular_reader(root_fd: int, relative: str):
    """Walk directory FDs without following symlinks; special files never block."""
    parts = path_parts(relative)
    directory = os.dup(root_fd)
    leaf = None
    try:
        for component in parts[:-1]:
            nxt = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=directory)
            os.close(directory)
            directory = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                       dir_fd=directory)
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise ValueError('Not a regular file')
        with os.fdopen(leaf, 'rb') as stream:
            leaf = None
            yield stream
    finally:
        if leaf is not None:
            os.close(leaf)
        os.close(directory)


def verify(root: Path, *, allow_legacy: bool = False) -> dict:
    if os.name != 'posix' or os.open not in os.supports_dir_fd:
        raise ValueError('This safe-open verifier requires POSIX directory descriptors')
    root = root.resolve(strict=True)
    current = root / 'SHA256SUMS.json'
    legacy = not (current.exists() or current.is_symlink())
    if legacy and not allow_legacy:
        raise ValueError('Current manifest missing; legacy verification requires --allow-legacy')
    name = 'RELEASE_MANIFEST.json' if legacy else 'SHA256SUMS.json'
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with regular_reader(directory, name) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('Manifest exceeds 16 MiB')
        obj = strict_json(raw.decode('utf8'))
        files = obj.get('files') if isinstance(obj, dict) else None
        if not isinstance(files, dict) or not files:
            raise ValueError('Manifest must contain a nonempty files mapping')
        errors = []
        for relative, expected in files.items():
            try:
                path_parts(relative)
            except ValueError:
                errors.append({'file': relative, 'error': 'unsafe_path'})
                continue
            if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
                errors.append({'file': relative, 'error': 'invalid_digest'})
                continue
            try:
                digest = hashlib.sha256()
                with regular_reader(directory, relative) as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(block)
                if digest.hexdigest() != expected:
                    errors.append({'file': relative, 'error': 'hash_mismatch'})
            except (OSError, ValueError):
                errors.append({'file': relative, 'error': 'missing_or_unsafe'})
    finally:
        os.close(directory)
    return {'manifest': name, 'legacy_opt_in_used': legacy,
            'checked_files': len(files), 'all_original_files_match': not errors, 'errors': errors,
            'unlisted_files_verified': False, 'cryptographic_signature_verified': False,
            'filesystem_atomic_snapshot_certified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--allow-legacy', action='store_true')
    args = parser.parse_args()
    try:
        result = verify(args.root, allow_legacy=args.allow_legacy)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({'all_original_files_match': False, 'error': type(exc).__name__}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['all_original_files_match'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
