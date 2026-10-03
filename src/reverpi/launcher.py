"""Native Pi launcher and immutable worker packing.

This module is NOT a sandbox. In research use Harbor/container isolation. Local
execution requires an explicit acknowledgment and uses a disposable environment.
A killed agent may have mutated files: the framework never auto-replays it.
"""
from __future__ import annotations
import asyncio
import json
import os
import platform
import shutil
import signal
import sys
import subprocess
import tarfile
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from .errors import LabError
from .processes import stop_process_group
from .util import atomic_write, canonical, digest, bytes_digest, process_lock, source_manifest


def node_version(binary: str) -> str:
    r=subprocess.run([binary,"--version"],capture_output=True,text=True,timeout=15,check=True)
    value=r.stdout.strip().removeprefix("v")
    parts=tuple(map(int,value.split(".")[:3]))
    if parts < (22,19,0):raise ValueError("Native Pi requires Node >=22.19.0; offline syntax tests on an older Node are not runtime certification")
    return value


def pi_paths(root: Path):
    node=shutil.which("node")
    cli=root/"pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    manifest=cli.parent.parent/"package.json"
    if not node or not cli.exists() or not manifest.exists():
        raise ValueError("Install Pi dependencies first: cd pi && npm install && npm run typecheck")
    version=node_version(node)
    installed=json.loads(manifest.read_text())["version"]
    pinned=json.loads((root/"pi/package.json").read_text())["dependencies"]["@earendil-works/pi-coding-agent"]
    if installed != pinned:raise ValueError("Installed Pi differs from the package's frozen candidate version")
    return node,cli,version,installed


def settings(effort: str, max_output_tokens: int, context_window: int = 131072):
    reserve = max(max_output_tokens + 1024, min(16384, context_window // 4))
    if reserve >= context_window:
        raise ValueError("Model context is too small for the frozen output reservation")
    keep = min(20000, max(256, (context_window - reserve) // 2))
    return {"defaultProvider":"rever-gateway","defaultThinkingLevel":effort,"quietStartup":True,
            "retry":{"enabled":False,"maxRetries":0,"provider":{"maxRetries":0}},
            "compaction":{"enabled":True,"reserveTokens":reserve,"keepRecentTokens":keep}}


def command(node, cli, extension, session_path, model, effort, *, mode="json"):
    if mode not in {"json", "rpc"}:raise ValueError("Unsupported Pi launch mode")
    argv = [str(node),str(cli),"--offline","--mode",mode,"--provider","rever-gateway","--model",model,
            "--thinking",effort,"--session",str(session_path),"--no-extensions","--no-skills","--no-prompt-templates",
            "--no-themes","--no-context-files","--no-approve","--extension",str(extension)]
    return argv + (["--print"] if mode == "json" else [])


def prompt_input(task: str) -> str:
    if not task.strip() or len(task.encode("utf-8")) > 1_000_000:
        raise ValueError("Task prompt empty or too large")
    return canonical({"schema": 1, "prompt": task})


def isolated_env(out: Path, gateway: str, token: str, max_tools: int, max_turns: int, private_http=False):
    # Never copy os.environ wholesale: provider credentials must not reach shell tools.
    keep={k:os.environ[k] for k in ("PATH","LANG","LC_ALL","TERM","TZ") if k in os.environ}
    keep.update(HOME=str(out/"home"),PI_CODING_AGENT_DIR=str(out/"agent-config"),PI_OFFLINE="1",
                REVER_GATEWAY_URL=gateway,REVER_SESSION_TOKEN=token,REVER_MAX_TOOLS=str(max_tools),
                REVER_MAX_TURNS=str(max_turns),REVER_ALLOW_PRIVATE_HTTP="1" if private_http else "0",
                NO_COLOR="1")
    return keep


def verifier_environment(root: Path, workspace: Path, registry: Path | None,
                         expected_sha: str | None, max_calls: int = 3) -> tuple[dict, dict | None]:
    """Explicit local opt-in; registry must not be task-writable in deployment.

    No hidden benchmark verifier is allowed here. The caller owns sandboxing.
    Capture this capability identically for baseline and candidate comparisons.
    """
    if registry is None and expected_sha is None:
        return {}, None
    if registry is None or expected_sha is None or type(max_calls) is not int or not 1 <= max_calls <= 10:
        raise ValueError('A SHA-pinned registry and 1..10 revalidation calls are required')
    from .revalidation import load_registry
    registry = registry.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    if registry.is_relative_to(workspace):
        raise ValueError('Operator registry must be outside the mutable task workspace')
    specs = load_registry(registry, expected_sha)
    if not specs:
        raise ValueError('No public verifier registered')
    runner = (root/'src/reverpi/revalidation.py').resolve(strict=True)
    runner_sha = bytes_digest(runner.read_bytes())
    python = Path(sys.executable).resolve(strict=True)
    env = {'REVER_ENABLE_REVALIDATION':'1', 'REVER_VERIFIER_REGISTRY':str(registry),
           'REVER_VERIFIER_REGISTRY_SHA256':expected_sha, 'REVER_VERIFIER_RUNNER':str(runner),
           'REVER_VERIFIER_RUNNER_SHA256':runner_sha, 'REVER_VERIFIER_PYTHON':str(python),
           'REVER_MAX_REVALIDATIONS':str(max_calls)}
    identity = {'registry_sha256':expected_sha, 'runner_sha256':runner_sha,
                'interpreter_sha256':bytes_digest(python.read_bytes()), 'max_calls':max_calls,
                'verifier_ids':sorted(specs), 'hidden_benchmark_verifier_access':False,
                'operator_readonly_mount_required':True, 'sandbox_provided':False}
    return env, identity


async def launch(root: Path,cwd: Path,prompt: Path,out: Path,gateway: str,token_file: Path,*,wall_seconds=1800,
                 max_tools=200,max_turns=80,acknowledge_unsandboxed=False,private_http=False,
                 verifier_registry: Path | None=None,verifier_registry_sha: str | None=None,max_revalidations=3):
    if not acknowledge_unsandboxed:
        raise ValueError("Local Pi has filesystem/shell access. Use a sandbox or explicitly pass --acknowledge-unsandboxed on a disposable workspace")
    if os.name!="posix" or not 1<=wall_seconds<=86400 or max_tools<1 or max_turns<1:raise ValueError("Invalid platform or execution limits")
    u=urlsplit(gateway)
    if u.username or u.password or u.query or u.fragment or u.scheme not in {"https","http"}:
        raise ValueError("Invalid gateway URL")
    if u.scheme=="http" and u.hostname not in {"127.0.0.1","localhost","::1"} and not private_http:
        raise ValueError("Private network HTTP requires --private-http")
    root,cwd,out=root.resolve(),cwd.resolve(),out.resolve()
    if not cwd.is_dir():raise ValueError("Task working directory is absent")
    if root==cwd or root in cwd.parents or cwd in root.parents:
        raise ValueError("Task workspace must be separate from the research code, keys and gold")
    node,cli,nversion,pversion=pi_paths(root)
    verifier_env, verifier_identity = verifier_environment(root,cwd,verifier_registry,verifier_registry_sha,max_revalidations)
    task=prompt.read_bytes().decode("utf-8")
    encoded_task=prompt_input(task)
    token=token_file.read_text().strip()
    out.mkdir(parents=True,exist_ok=True,mode=0o700)
    with process_lock(out/"writer.lock"):
        status_path=out/"task.json"
        if status_path.exists():raise LabError("task_already_started","Native tasks are not auto-replayed; restore an independent snapshot and create a new run")
        async with httpx.AsyncClient(timeout=15,trust_env=False,follow_redirects=False) as h:
            response=await h.get(gateway.rstrip("/")+"/session",headers={"Authorization":"Bearer "+token})
            response.raise_for_status();info=response.json()
        if info["revision"]!=0 or info["accounting"]["attempts"]:
            raise ValueError("The session is not fresh")
        (out/"home").mkdir(mode=0o700);(out/"agent-config").mkdir(mode=0o700)
        atomic_write(out/"agent-config/settings.json",canonical(settings(info["effort"],info["max_output_tokens"],info["context_window"])))
        atomic_write(out/"prompt.txt",task)
        atomic_write(out/"prompt-input.json",encoded_task)
        state={"status":"starting","task_sha":digest(task),"method":info["method"],"model":info["model"],
               "effort":info["effort"],"node":nversion,"pi":pversion,"source_sha":digest(source_manifest(root)),
               "workspace":str(cwd),"revalidation_capability":verifier_identity,"official_task_success":None,"mock_provider":info["mock"],"started":time.time()}
        atomic_write(status_path,canonical(state))
        env=isolated_env(out,gateway,token,max_tools,max_turns,private_http)
        env["REVER_PROMPT_TRANSPORT"]="json_v1"
        env.update(verifier_env)
        proc=None;tasks=[];events={"agent_end":False,"model_error":False,"invalid_json_lines":0}
        async def drain(pipe,target,parse=False):
            total=0;buffer=b""
            with target.open("wb") as f:
                os.chmod(target,0o600)
                while part:=await pipe.read(8192):
                    total+=len(part)
                    if total>100*1024*1024:raise LabError("log_quota","Native agent log exceeded 100 MiB")
                    f.write(part);f.flush()
                    if parse:
                        buffer+=part
                        while b"\n" in buffer:
                            line,buffer=buffer.split(b"\n",1)
                            if not line.strip():continue
                            try:
                                event=json.loads(line)
                                if event.get("type")=="agent_end":events["agent_end"]=True
                                if event.get("type")=="extension_error" or (event.get("type")=="message_end" and isinstance(event.get("message"),dict) and event["message"].get("stopReason") in {"error","aborted","length"}):
                                    events["model_error"]=True
                            except (ValueError,AttributeError):events["invalid_json_lines"]+=1
                    if len(buffer)>32*1024*1024:raise LabError("event_quota","Native event exceeds line limit")
                if parse and buffer.strip():events["invalid_json_lines"]+=1
        try:
            with (out/"prompt-input.json").open("rb") as task_input:
                proc=await asyncio.create_subprocess_exec(*command(node,cli,root/"pi/src/index.ts",out/"session.jsonl",info["model"],info["effort"]),
                    cwd=cwd,env=env,stdin=task_input,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,start_new_session=True)
            state.update(status="running",pid=proc.pid);atomic_write(status_path,canonical(state))
            tasks=[asyncio.create_task(drain(proc.stdout,out/"events.jsonl",True)),asyncio.create_task(drain(proc.stderr,out/"stderr.log")),asyncio.create_task(proc.wait())]
            await asyncio.wait_for(asyncio.gather(*tasks),wall_seconds)
            state.update(status="completed" if proc.returncode==0 and events["agent_end"] and not events["model_error"] and not events["invalid_json_lines"] else "execution_failed",return_code=proc.returncode)
        except TimeoutError:
            state.update(status="timeout",possible_partial_tool_writes=True)
        except asyncio.CancelledError:
            state.update(status="interrupted",possible_partial_tool_writes=True);raise
        except BaseException as err:
            state.update(status="infrastructure_error",error_kind=type(err).__name__,possible_partial_tool_writes=True);raise
        finally:
            await stop_process_group(proc)
            for t in tasks:
                if not t.done():t.cancel()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            state.update(events=events,finished=time.time())
            atomic_write(status_path,canonical(state))
        return state


def prepare_worker(root: Path,out: Path):
    if platform.system()!="Linux":raise ValueError("Prepare the worker on the same Linux architecture as benchmark containers")
    node,cli,nversion,pversion=pi_paths(root)
    if out.exists():raise ValueError("Worker output already exists")
    build=out.with_suffix(".build")
    if build.exists():raise ValueError("Worker build staging path already exists")
    build.mkdir(parents=True,mode=0o700)
    try:
        (build/"node/bin").mkdir(parents=True)
        shutil.copy2(Path(node).resolve(),build/"node/bin/node")
        (build/"pi").mkdir()
        for name in ("src","node_modules"):
            shutil.copytree(root/"pi"/name,build/"pi"/name,symlinks=True)
        for name in ("package.json","package-lock.json"):
            if (root/"pi"/name).exists():shutil.copy2(root/"pi"/name,build/"pi"/name)
        for p in build.rglob("*"):
            if p.is_symlink() and not p.resolve().is_relative_to(build.resolve()):
                raise ValueError("Worker contains a symlink outside the packed root")
        manifest={"node":nversion,"pi":pversion,"architecture":platform.machine(),"source_sha":digest(source_manifest(root)),
                  "glibc_requirement":"Verify on each environment; Alpine/musl and foreign architectures are not assumed supported"}
        atomic_write(build/"WORKER.json",canonical(manifest),mode=0o644)
        out.parent.mkdir(parents=True,exist_ok=True)
        with tarfile.open(out,"w:gz",dereference=False) as tar:tar.add(build,arcname="reverpi-worker")
        manifest["archive_sha256"]=bytes_digest(out.read_bytes())
        atomic_write(Path(str(out)+".json"),canonical(manifest));return manifest
    finally:shutil.rmtree(build,ignore_errors=True)
