"""Offline-only research commands; no provider credentials, paid calls, or retries."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
from .research_audit import audit_ledger, audit_sessions, TrialObservation, diagnose_trial
from .risk_calibration import PairedCluster, calibrate
from .evidence_policy import Evidence, plan_evidence
from .util import strict_json_loads


def read_json(path: str):
    return strict_json_loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ledger", "activation"):
        sub = commands.add_parser(name)
        sub.add_argument("--db", required=True, help="Existing SQLite file; a wrong path is never created")
        if name == "ledger":
            sub.add_argument("--immutable-snapshot", action="store_true", help="Frozen checkpointed copies only; refuses nonempty WAL")
    for name in ("diagnose", "evidence-plan"):
        sub = commands.add_parser(name)
        sub.add_argument("--input", required=True)
    sub = commands.add_parser("calibrate")
    sub.add_argument("--input", required=True, help="JSON array, one prespecified pair per independent cluster")
    sub.add_argument("--policies", type=int, required=True)
    sub.add_argument("--alpha", type=float, default=.05)
    sub = commands.add_parser("delivery")
    sub.add_argument("--root", required=True)
    sub.add_argument("--manifest", default="MANIFEST_DELIVERY.json")
    sub = commands.add_parser("review-state")
    sub.add_argument("--root", required=True)
    sub.add_argument("--run", required=True)
    for name in ("native-run", "native-acceptance"):
        sub = commands.add_parser(name)
        sub.add_argument("--run", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "delivery":
            from .bundle_audit import audit_delivery
            result = audit_delivery(Path(args.root), args.manifest)
        elif args.command == "review-state":
            from .bundle_audit import audit_review
            result = audit_review(Path(args.root), Path(args.run))
        elif args.command == "native-run":
            from .bundle_audit import audit_native_run
            result = audit_native_run(Path(args.run))
        elif args.command == "native-acceptance":
            from .bundle_audit import audit_acceptance
            result = audit_acceptance(Path(args.run))
        elif args.command == "ledger":
            result = audit_ledger(Path(args.db), immutable_snapshot=args.immutable_snapshot)
        elif args.command == "activation":
            result = audit_sessions(Path(args.db))
        elif args.command == "diagnose":
            result = diagnose_trial(TrialObservation(**read_json(args.input)))
        elif args.command == "calibrate":
            data = read_json(args.input)
            if not isinstance(data, list):
                raise ValueError("Expected a JSON array of independent pairs")
            result = calibrate([PairedCluster(**row) for row in data], alpha=args.alpha, policies=args.policies)
        else:
            data = read_json(args.input)
            if set(data) - {"evidence", "current", "require_current"}:
                raise ValueError("Unexpected evidence-plan fields")
            result = plan_evidence(Evidence(**data["evidence"]), data["current"],
                                   require_current=data.get("require_current", True))
    except Exception as exc:
        # Do not dump payloads, bearer keys, or raw usage objects in an error traceback.
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
