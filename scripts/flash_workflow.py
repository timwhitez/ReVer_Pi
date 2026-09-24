#!/usr/bin/env python3
"""Single-model, budgeted entry point. No command launches full Terminal-Bench."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from reverpi.config import Provider, load
from reverpi.errors import LabError
from reverpi.lean import (init_campaign, load_campaign, prepare_dev, prepare_campaign_review,
                         run_slot, campaign_status, audit_gen10, campaign_preflight)
from reverpi.lean_subset import freeze_subset, compare_subset
from reverpi.util import canonical, strict_json_loads, digest


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='cmd', required=True)
    a = commands.add_parser('audit-gen10')
    a.add_argument('--repo', type=Path, default=ROOT)
    a = commands.add_parser('init')
    a.add_argument('--provider', type=Path, required=True)
    a.add_argument('--out', type=Path, required=True)
    a.add_argument('--dev-tokens', type=int, default=90000)
    a.add_argument('--review-tokens', type=int, default=120000)
    a.add_argument('--max-review-jobs', type=int, default=8)
    a.add_argument('--authorize-paid-campaign', action='store_true', help='Only with NEW explicit operator budget authority; never reuse an old balance')
    for name in ('prepare-dev', 'prepare-review', 'run', 'status', 'preflight'):
        a = commands.add_parser(name)
        a.add_argument('--campaign', type=Path, required=True)
        a.add_argument('--campaign-sha256', required=True)
        if name == 'preflight':
            a.add_argument('--slot', choices=['dev-01', 'dev-02', 'review'], required=True)
        if name == 'prepare-dev':
            a.add_argument('--slot', choices=['dev-01', 'dev-02'], required=True)
            a.add_argument('--gold-certificate', type=Path)
            for key in ('parent', 'workspace', 'registry'):
                a.add_argument('--' + key, type=Path, required=True)
            for key in ('parent-sha256', 'registry-sha256', 'gold-sha256', 'source-note'):
                a.add_argument('--' + key, required=True)
        if name == 'prepare-review':
            a.add_argument('--scope', type=Path, required=True)
        if name == 'run':
            a.add_argument('--slot', choices=['dev-01', 'dev-02', 'review'], required=True)
            a.add_argument('--allow-paid', action='store_true')
            a.add_argument('--acknowledge-unsandboxed', action='store_true')
    a = commands.add_parser('check-gold', help='Controller-only, zero-model schema check; never copies answers to run')
    for key in ('parent', 'gold', 'out'):
        a.add_argument('--' + key, type=Path, required=True)
    for key in ('parent-sha256', 'gold-sha256'):
        a.add_argument('--' + key, required=True)
    a = commands.add_parser('budget-screen', help='Read-only stress screen for a known archived or prepared plan')
    a.add_argument('--run', type=Path, required=True)
    a.add_argument('--plan-sha256', required=True)
    a = commands.add_parser('subset')
    a.add_argument('--inventory', type=Path, required=True)
    a.add_argument('--out', type=Path, required=True)
    a.add_argument('--n', type=int, default=24)
    a.add_argument('--seed', type=int, default=20260919)
    a = commands.add_parser('compare')
    a.add_argument('--plan', type=Path, required=True)
    a.add_argument('--plan-sha256', required=True)
    a.add_argument('--results', type=Path, required=True)
    a = commands.add_parser('export')
    a.add_argument('--run', type=Path, required=True)
    a.add_argument('--out', type=Path, required=True)
    for action in ("preflight", "run"):
        commands.choices[action].add_argument('--slot-binding-sha256',
            help='Operator-held digest returned at slot preparation; never auto-derived at dispatch')
    args = p.parse_args()
    try:
        if args.cmd == 'check-gold':
            from reverpi.preflight import certify_gold
            result = certify_gold(args.parent, args.parent_sha256, args.gold, args.gold_sha256, args.out)
        elif args.cmd == 'budget-screen':
            from reverpi.preflight import budget_screen
            screen_plan = strict_json_loads((args.run/'plan.json').read_bytes())
            if digest(screen_plan) != args.plan_sha256:
                raise ValueError('Budget-screen plan identity mismatch')
            result = budget_screen(args.run, screen_plan)
        elif args.cmd == 'preflight':
            result = campaign_preflight(ROOT, args.campaign, args.campaign_sha256, args.slot, expected_binding_sha=args.slot_binding_sha256)
        elif args.cmd == 'audit-gen10':
            result = audit_gen10(args.repo)
        elif args.cmd == 'init':
            result = init_campaign(ROOT, args.out, load(args.provider, Provider),
                                   dev_tokens=args.dev_tokens, review_tokens=args.review_tokens,
                                   max_review_jobs=args.max_review_jobs,
                                   paid_execution_authorized=args.authorize_paid_campaign)
        elif args.cmd == 'prepare-dev':
            result = prepare_dev(ROOT, args.campaign, args.campaign_sha256, args.slot,
                                 parent=args.parent, parent_sha=args.parent_sha256, workspace=args.workspace,
                                 registry=args.registry, registry_sha=args.registry_sha256,
                                 gold_sha=args.gold_sha256, source_note=args.source_note,
                                 gold_certificate=args.gold_certificate)
        elif args.cmd == 'prepare-review':
            result = prepare_campaign_review(ROOT, args.campaign, args.campaign_sha256,
                                             strict_json_loads(args.scope.read_bytes()))
        elif args.cmd == 'run':
            result = asyncio.run(run_slot(ROOT, args.campaign, args.campaign_sha256, args.slot,
                                          allow_paid=args.allow_paid,
                                          acknowledge_unsandboxed=args.acknowledge_unsandboxed, expected_binding_sha=args.slot_binding_sha256))
        elif args.cmd == 'status':
            result = campaign_status(ROOT, args.campaign, args.campaign_sha256, write=True)
        elif args.cmd == 'subset':
            result = freeze_subset(strict_json_loads(args.inventory.read_bytes()), args.out,
                                   n=args.n, seed=args.seed)
        elif args.cmd == 'export':
            from reverpi.lean_export import export_run
            result = export_run(args.run, args.out)
        else:
            result = compare_subset(strict_json_loads(args.plan.read_bytes()), args.plan_sha256,
                                    strict_json_loads(args.results.read_bytes()))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.cmd == 'preflight' and not result['ready_for_paid_dispatch']:
            return 2
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(canonical({'error': 'interrupted', 'message': 'Execution interrupted; preserve ledgers and frozen outputs. No retry is authorized.'}), file=sys.stderr)
        return 130
    except (LabError, OSError, ValueError, KeyError, TypeError) as exc:
        print(canonical({'error': exc.kind if isinstance(exc, LabError) else type(exc).__name__,
                         'local_detail': exc.message if isinstance(exc, LabError) and exc.kind in {'configuration_preflight','evaluation_preflight','campaign_preflight'} else None,
                         'message': 'Stopped; preserve the frozen plan, all ledgers and partial outputs. '
                                    'Do not change model, recreate slots, or rewrite results to bypass this stop.'}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
