#!/usr/bin/env python3
"""Run local checks without installing packages or calling any model provider."""
from __future__ import annotations
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, help='NEW directory for immutable check logs')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    checks = {}
    env = dict(os.environ)
    npm = shutil.which('npm')
    if npm:
        discovery = subprocess.run([npm, 'root', '-g'], capture_output=True, text=True, timeout=15)
        if discovery.returncode == 0:
            # Needed by contract tests on the author's offline validation machine.
            env.setdefault('NODE_PATH', discovery.stdout.strip())
    jobs = [('python', [sys.executable, '-m', 'pytest', '-q'], root),
            ('compileall', [sys.executable, '-m', 'compileall', '-q', 'src', 'tests', 'scripts'], root),
            ('node_contracts', ['node', '--experimental-strip-types', '--test',
                'tests/core.test.mjs', 'tests/extension-contract.test.mjs'], root/'pi'),
            ('real_typecheck', ['npm', 'run', 'typecheck'], root/'pi')]
    for name, command, cwd in jobs:
        try:
            result = subprocess.run(command, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, timeout=180)
            (out/f'{name}.log').write_text(result.stdout, encoding='utf-8')
            checks[name] = {'exit_code': result.returncode, 'passed': result.returncode == 0, 'command': command}
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks[name] = {'passed': False, 'error': type(exc).__name__, 'command': command}
    node = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=10).stdout.strip() if shutil.which('node') else None
    status = {'schema': 'reverpi.local-checks.v1', 'python': platform.python_version(), 'node': node,
        'docker_executable_present': bool(shutil.which('docker')), 'checks': checks,
        'paid_model_calls': 0, 'native_docker_benchmark_executed': False,
        'independent_review_completed': False, 'external_evaluation_completed': False,
        'native_acceptance_certified': False}
    (out/'validation.json').write_text(json.dumps(status, indent=2), encoding='utf-8')
    print(json.dumps(status, indent=2))
    return 0 if all(c['passed'] for c in checks.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
