from __future__ import annotations
import argparse
import asyncio
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from .config import Provider, StudyConfig, Budget, load
from .errors import LabError
from .util import atomic_write, canonical, digest, process_lock, source_manifest

ROOT = Path(__file__).resolve().parents[2]


def allow_provider(args):
    p = load(args.provider, Provider)
    if not p.mock and not args.allow_paid:
        raise ValueError("Real Provider calls require --allow-paid; keys are only read from environment variables")
    p.secret()
    return p


def doctor(args):
    packages = {}
    for name in ("httpx","pydantic","PyYAML","fastapi","uvicorn","pytest","harbor"):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    info = {"python":sys.version.split()[0],"platform":platform.platform(),"packages":packages,
            "executables":{n:shutil.which(n) for n in ("node","npm","docker","harbor")},
            "network_called":False,"code_sha":digest(source_manifest(ROOT)),
            "pi_package_installed":(ROOT/"pi/node_modules/@earendil-works/pi-coding-agent/package.json").exists()}
    if args.provider:
        p=load(args.provider,Provider)
        info["provider"]={"protocol":p.protocol,"model":p.model,"mock":p.mock,"requested_effort":p.effort,
                          "mapped_effort":p.effort_map[p.effort],"secret_present":p.mock or bool(os.environ.get(p.api_key_env)),
                          "currency_pricing_configured":p.prices.configured,"compatibility":"NOT_VERIFIED_WITH_LIVE_PROVIDER"}
    if args.out: atomic_write(Path(args.out),canonical(info))
    return info


def parser():
    p=argparse.ArgumentParser(prog="reverpi",description="ReVer-Pi research lab: explicit budgets, protocols, provenance and failure accounting")
    sub=p.add_subparsers(dest="command",required=True)
    d=sub.add_parser("doctor",help="Offline environment/config inspection; no paid request")
    d.add_argument("--provider");d.add_argument("--out")
    sub.add_parser("methods",help="List implemented algorithms, not the 40-item literature inventory")
    f=sub.add_parser("fixtures",help="Generate SEARCH-ONLY protocol fixtures, never an external benchmark")
    f.add_argument("--out",default="data/fixtures");f.add_argument("--count",type=int,default=12)
    a=sub.add_parser("audit-data");a.add_argument("--public",nargs="+",required=True);a.add_argument("--out",default="reports/data-audit.json")
    z=sub.add_parser("freeze");z.add_argument("--config",required=True);z.add_argument("--public",required=True);z.add_argument("--gold",required=True)
    z.add_argument("--all-public",nargs="+",required=True);z.add_argument("--out",required=True)
    z.add_argument("--provider",required=True);z.add_argument("--probe",required=True);z.add_argument("--review-manifest",required=True)
    for name in ("run","probe","review","gateway"):
        x=sub.add_parser(name)
        x.add_argument("--provider",required=True);x.add_argument("--config",default="configs/pilot.yaml")
        x.add_argument("--out",required=True);x.add_argument("--allow-paid",action="store_true")
        if name=="run":
            x.add_argument("--public",required=True);x.add_argument("--gold",required=True);x.add_argument("--acknowledge-heldout",action="store_true")
        if name=="review":
            x.add_argument("--rounds",type=int,choices=[2,3],default=3)
            x.add_argument("--plan-only",action="store_true")
            x.add_argument("--in-doubt-policy",choices=["halt","quarantine_and_continue"],default="halt",
                           help="Freeze before running. Never resends an ambiguous op; quarantine keeps a failing gate.")
        if name=="gateway":
            x.add_argument("--host",default="127.0.0.1");x.add_argument("--port",type=int,default=8765);x.add_argument("--allow-network",action="store_true")
    s=sub.add_parser("session-create");s.add_argument("--run",required=True);s.add_argument("--id",required=True);s.add_argument("--method",required=True)
    s.add_argument("--token-file",required=True);s.add_argument("--ttl",type=int,default=86400)
    s=sub.add_parser("session-revoke");s.add_argument("--run",required=True);s.add_argument("--id",required=True)
    an=sub.add_parser("analyze");an.add_argument("--run",required=True);an.add_argument("--baseline",default="full");an.add_argument("--unseal",action="store_true")
    an.add_argument("--out")
    co=sub.add_parser("costs");co.add_argument("--run",required=True);co.add_argument("--config",default="configs/pilot.yaml")
    re=sub.add_parser("reconcile");re.add_argument("--run",required=True);re.add_argument("--config",default="configs/pilot.yaml")
    re.add_argument("--attempt",required=True,type=int);re.add_argument("--tokens",required=True,type=int);re.add_argument("--usd",required=True,type=float);re.add_argument("--evidence",required=True)
    pi=sub.add_parser("pi-run");pi.add_argument("--cwd",required=True);pi.add_argument("--prompt-file",required=True);pi.add_argument("--out",required=True)
    pi.add_argument("--gateway",default="http://127.0.0.1:8765");pi.add_argument("--token-file",required=True)
    pi.add_argument("--wall-seconds",type=int,default=1800);pi.add_argument("--max-tools",type=int,default=200);pi.add_argument("--max-turns",type=int,default=80)
    pi.add_argument("--acknowledge-unsandboxed",action="store_true");pi.add_argument("--private-http",action="store_true")
    pi.add_argument("--verifier-registry");pi.add_argument("--verifier-registry-sha256")
    pi.add_argument("--max-revalidations",type=int,default=3)
    w=sub.add_parser("prepare-worker");w.add_argument("--out",required=True)
    dt=sub.add_parser("dev-tasks",help="Generate and verify six owned SEARCH-only Harbor smoke tasks")
    dt.add_argument("--out",default="data/owned-tasks");dt.add_argument("--image",default="python:3.11-slim")
    ac=sub.add_parser("native-acceptance",help="Real Pi RPC tool/compaction gate; not a benchmark")
    ac.add_argument("--gateway-run",required=True);ac.add_argument("--gateway",default="http://127.0.0.1:8765")
    ac.add_argument("--out",required=True);ac.add_argument("--methods",nargs="+",default=["pi_original","pi_native","mask","rever_lite"])
    ac.add_argument("--allow-paid",action="store_true");ac.add_argument("--acknowledge-unsandboxed",action="store_true")
    ac.add_argument("--require-recovery",action="store_true",help="Verify an actual exact recover_evidence result for custom compactors")
    ac.add_argument("--private-http",action="store_true");ac.add_argument("--deadline",type=int,default=600)
    bp=sub.add_parser("benchmark-plan",help="Freeze native task trees, source identities, methods, code and worker")
    bp.add_argument("--tasks",required=True);bp.add_argument("--config",default="configs/native-pilot.yaml")
    bp.add_argument("--provider",required=True);bp.add_argument("--gateway-config",default="configs/gateway.yaml")
    bp.add_argument("--worker",required=True);bp.add_argument("--out",required=True)
    bp.add_argument("--probe");bp.add_argument("--review-manifest");bp.add_argument("--native-acceptance")
    br=sub.add_parser("benchmark-run",help="Execute a frozen sequential Harbor matrix, never silently retry trials")
    br.add_argument("--plan",required=True);br.add_argument("--gateway-run",required=True);br.add_argument("--gateway",required=True)
    br.add_argument("--worker",required=True);br.add_argument("--out",required=True)
    br.add_argument("--allow-paid",action="store_true");br.add_argument("--acknowledge-heldout",action="store_true")
    bc=sub.add_parser("benchmark-collect",help="Collect existing official results or mark orphans; executes no tasks")
    bc.add_argument("--run",required=True);bc.add_argument("--gateway-run",required=True)
    bc.add_argument("--mark-interrupted",action="store_true");bc.add_argument("--acknowledge-cleanup",action="store_true")
    ds=sub.add_parser("development-selection",help="Development-only Pareto screen; never reads held-out results")
    ds.add_argument("--run",required=True);ds.add_argument("--out",required=True);ds.add_argument("--baseline",default="full")
    ds.add_argument("--quality-tolerance",type=float,default=0.0)
    return p


def execute(args):
    from .data import make_fixtures,audit,freeze
    from .ledger import Ledger
    from .memory import METHODS
    cmd=args.command
    if cmd=="doctor":return doctor(args)
    if cmd=="methods":return {"checkpoint_methods":METHODS,"pi_original":"Actual Pi compactor without added recovery tool; standardized Provider/accounting wrapper", "pi_native":"Actual Pi compactor with the SAME recovery capability as compressed methods","native_responses_compact":"optional opaque API primitive, outside comparable low-effort budget-capped track"}
    if cmd=="fixtures":
        if not 1<=args.count<=1000:raise ValueError("Fixture count must be 1..1000")
        make_fixtures(Path(args.out),args.count);return {"out":args.out,"external_benchmark":False}
    if cmd=="audit-data":
        report=audit([Path(x) for x in args.public]);atomic_write(Path(args.out),canonical(report));return report
    if cmd=="freeze":
        provider=load(args.provider,Provider)
        from .review import check_review
        pr=json.loads(Path(args.probe).read_text());rr=check_review(Path(args.review_manifest),ROOT)
        if provider.mock or pr.get("mock") or not pr.get("compatible_for_this_probe"):
            raise ValueError("Formal freeze requires a successful real Provider probe")
        if pr.get("provider_sha")!=digest(provider.model_dump()):
            raise ValueError("Probe does not match the exact frozen Provider profile")
        if rr.get("source_sha")!=digest(source_manifest(ROOT)) or not rr.get("eligible_for_human_acceptance"):
            raise ValueError("Formal freeze requires completed non-mock reviews for this exact code hash")
        report=freeze(ROOT,Path(args.config),Path(args.public),Path(args.gold),[Path(x) for x in args.all_public],Path(args.out))
        report.update(provider_sha=digest(provider.model_dump()),probe_sha=digest(pr),review_sha=digest(rr))
        atomic_write(Path(args.out),canonical(report));return {"frozen":args.out,"tasks":len(report["task_ids"]),"source_sha":report["code_sha"]}
    if cmd in {"run","probe","review","gateway"}:
        if cmd=="review" and args.plan_only:
            from .review import review_jobs
            m,jobs=review_jobs(ROOT,args.rounds)
            return {"jobs":len(jobs),"source_sha":digest(m),"source_bytes_over_all_rounds":sum(len(j["source"].encode()) for j in jobs),"paid_calls":0}
        provider=allow_provider(args);config=load(args.config,StudyConfig)
        out=Path(args.out)
        if cmd=="run":
            from .study import run_study
            if config.split in {"gate","external"}:
                freeze_doc=json.loads(Path(config.freeze_manifest or "MISSING_FREEZE").read_text())
                if freeze_doc.get("provider_sha")!=digest(provider.model_dump()):raise ValueError("Frozen Provider profile mismatch")
            return asyncio.run(run_study(ROOT,provider,config,Path(args.public),Path(args.gold),out,config_path=Path(args.config),acknowledge_heldout=args.acknowledge_heldout))
        if cmd=="probe":
            from .probe import probe
            return asyncio.run(probe(provider,config.budget,out))
        if cmd=="review":
            from .review import run_reviews
            return asyncio.run(run_reviews(ROOT,provider,config.budget,out,rounds=args.rounds,in_doubt_policy=args.in_doubt_policy))
        if args.host not in {"127.0.0.1","localhost","::1"} and not args.allow_network:
            raise ValueError("Non-loopback binding requires --allow-network and network-level isolation")
        import uvicorn
        from .gateway import create_app
        with process_lock(out/"gateway.lock"):
            # Operational choice, not a proven repair for historical fetch failures.
            # Access-log HTTP status does not prove complete response delivery;
            # correlate request IDs and client/body completion before attributing loss.
            uvicorn.run(create_app(provider,config,out),host=args.host,port=args.port,workers=1,
                        access_log=True,timeout_keep_alive=120)
        return {"gateway":"stopped"}
    if cmd.startswith("session-"):
        from .gateway import Sessions
        sessions=Sessions(Path(args.run)/"sessions.sqlite")
        if cmd=="session-revoke":sessions.disable(args.id);return {"revoked":args.id}
        if Path(args.token_file).exists():raise ValueError("Token output already exists")
        token=sessions.create(args.id,args.method,ttl=args.ttl)
        atomic_write(Path(args.token_file),token)
        return {"id":args.id,"method":args.method,"token_file":args.token_file,"token_not_printed":True}
    if cmd=="analyze":
        from .statistics import analyze
        run=Path(args.run);manifest=json.loads((run/"manifest.json").read_text())
        if manifest["study"]["split"] in {"gate","external"} and not args.unseal:
            raise ValueError("Held-out analysis requires explicit --unseal; scores are not development feedback")
        from .results import read_results, current_cost_view
        rows, cost_source = current_cost_view(run,manifest,read_results(run,manifest))
        report=analyze(rows,manifest["study"]["methods"],baseline=args.baseline)
        report.update(mock=manifest["mock"],track=manifest["track"],cost_source=cost_source,
                      result_integrity_checked=manifest.get("results_schema")==2)
        if manifest["track"] == "native_pi_harbor":
            report["note"] = "Official native task rewards, predetermined primary repeat=0. Missing trials remain unknown; cluster inference depends on curator provenance. Ledger costs exclude Docker/host billing."
        atomic_write(Path(args.out) if args.out else run/"analysis.json",canonical(report));return report
    if cmd in {"costs","reconcile"}:
        ledger=Ledger(Path(args.run)/"ledger.sqlite",load(args.config,StudyConfig).budget)
        if cmd=="reconcile":ledger.reconcile(args.attempt,args.tokens,args.usd,args.evidence)
        return ledger.totals()
    if cmd=="development-selection":
        from .selection import select_development
        return select_development(Path(args.run),Path(args.out),args.baseline,args.quality_tolerance)
    if cmd=="dev-tasks":
        from .devtasks import generate_tasks,validate_oracles
        result=generate_tasks(Path(args.out),args.image);validation=validate_oracles(Path(args.out))
        if not validation["all_valid"]:raise ValueError("Owned oracle pre/post verification failed")
        return {**result,"local_oracle_checks_passed":True,"native_environment_tested":False}
    if cmd=="native-acceptance":
        from .acceptance import native_acceptance
        return asyncio.run(native_acceptance(ROOT,Path(args.gateway_run),args.gateway,Path(args.out),args.methods,
            acknowledge_unsandboxed=args.acknowledge_unsandboxed,private_http=args.private_http,allow_paid=args.allow_paid,deadline=args.deadline,require_recovery=args.require_recovery))
    if cmd=="benchmark-plan":
        from .benchmark import make_plan,MatrixConfig
        return make_plan(ROOT,Path(args.tasks),load(args.config,MatrixConfig),load(args.provider,Provider),load(args.gateway_config,StudyConfig),Path(args.out),
            probe_path=Path(args.probe) if args.probe else None,review_path=Path(args.review_manifest) if args.review_manifest else None,
            worker_path=Path(args.worker),acceptance_path=Path(args.native_acceptance) if args.native_acceptance else None)
    if cmd=="benchmark-run":
        from .benchmark import run_matrix
        return asyncio.run(run_matrix(ROOT,Path(args.plan),Path(args.gateway_run),args.gateway,Path(args.worker),Path(args.out),
            allow_paid=args.allow_paid,acknowledge_heldout=args.acknowledge_heldout))
    if cmd=="benchmark-collect":
        from .benchmark import collect_matrix
        return collect_matrix(Path(args.run),Path(args.gateway_run),mark_interrupted=args.mark_interrupted,acknowledge_cleanup=args.acknowledge_cleanup)
    if cmd=="pi-run":
        from .launcher import launch
        return asyncio.run(launch(ROOT,Path(args.cwd),Path(args.prompt_file),Path(args.out),args.gateway,Path(args.token_file),
                                  wall_seconds=args.wall_seconds,max_tools=args.max_tools,max_turns=args.max_turns,
                                  acknowledge_unsandboxed=args.acknowledge_unsandboxed,private_http=args.private_http,
                                  verifier_registry=Path(args.verifier_registry) if args.verifier_registry else None,
                                  verifier_registry_sha=args.verifier_registry_sha256,max_revalidations=args.max_revalidations))
    if cmd=="prepare-worker":
        from .launcher import prepare_worker
        return prepare_worker(ROOT,Path(args.out))
    raise ValueError("Unknown command")


def main():
    try:
        args=parser().parse_args();result=execute(args)
        if result is not None:print(json.dumps(result,ensure_ascii=False,indent=2))
    except KeyboardInterrupt:
        print("Interrupted. Check reservations and task state before resuming.",file=sys.stderr);raise SystemExit(130)
    except (LabError,ValueError,OSError) as err:
        payload=err.record() if isinstance(err,LabError) else {"kind":type(err).__name__,"message":str(err)}
        print(json.dumps({"error":payload},ensure_ascii=False),file=sys.stderr);raise SystemExit(2)
