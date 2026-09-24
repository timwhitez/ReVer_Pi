"""Optional Harbor custom agent: requires real Harbor + prebuilt Linux worker.

API contract adapted from harbor.agents.base.BaseAgent and BaseEnvironment as
retrieved on 2026-09-12. Integration tests here use explicit contract doubles;
no completed public-benchmark run is claimed in this release.
"""
from __future__ import annotations
import json
import asyncio
import contextlib
import os
import shlex
import tempfile
import time
from pathlib import Path
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from .gateway import Sessions
from .launcher import command, settings
from .ledger import Ledger
from .protocols import usage_parts
from .config import Budget
from .util import atomic_write, canonical, digest, bytes_digest, safe_id


class ReVerPiAgent(BaseAgent):
    LOG_COLLECTION_TIMEOUT_SECONDS = 30
    SUPPORTS_ATIF=False
    SUPPORTS_RESUME=False
    SUPPORTS_WINDOWS=False

    def __init__(self,*args,gateway_run=None,gateway_url=None,worker_archive=None,method="rever_lite",wall_seconds=1800,cell_id=None,max_tools=200,max_turns=80,**kwargs):
        super().__init__(*args,**kwargs)
        self.gateway_run=Path(gateway_run or os.environ.get("REVER_GATEWAY_RUN", ""))
        self.gateway_url=gateway_url or os.environ.get("REVER_GATEWAY_URL","")
        self.worker=Path(worker_archive or os.environ.get("REVER_WORKER_ARCHIVE",""))
        if not self.gateway_url or not self.worker.is_file() or not (self.gateway_run/"ledger.sqlite").exists():
            raise ValueError("Set gateway_run, reachable gateway_url and a prepared worker_archive")
        self.method=method;self.wall_seconds=int(wall_seconds)
        self.sid=cell_id or "hb_"+digest({"logs":str(self.logs_dir.resolve()),"method":method})[:40]
        safe_id(self.sid)
        self.max_tools=int(max_tools);self.max_turns=int(max_turns)
        if not 1 <= self.max_tools <= 10000 or not 1 <= self.max_turns <= 10000 or not 10 <= self.wall_seconds <= 86400:
            raise ValueError("Trial step/time bounds are invalid")
        self.sessions=Sessions(self.gateway_run/"sessions.sqlite")
        self.token=None;self.runtime="/tmp/reverpi-"+self.sid
        self.started=False

    @staticmethod
    def name():return "reverpi"
    def version(self):return "0.1.0"

    def _ledger(self):
        # Read frozen budget, never widen it for a benchmark task.
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.gateway_run/"ledger.sqlite")) as db:
            budget=json.loads(db.execute("SELECT value FROM meta WHERE key='budget'").fetchone()[0])
        return Ledger(self.gateway_run/"ledger.sqlite",Budget.model_validate(budget))

    async def setup(self,environment:BaseEnvironment):
        meta_path=Path(str(self.worker)+".json")
        if not meta_path.is_file():raise ValueError("Worker needs its SHA/version manifest")
        meta=json.loads(meta_path.read_text())
        if meta.get("archive_sha256")!=bytes_digest(self.worker.read_bytes()):raise ValueError("Worker archive hash mismatch")
        with self._ledger().db() as db:
            gateway_sha=json.loads(db.execute("SELECT value FROM meta WHERE key='gateway_code'").fetchone()[0])
        if meta.get("source_sha") != gateway_sha:raise ValueError("Worker and gateway source hashes differ")
        self.token=self.sessions.create(self.sid,self.method,ttl=min(604800,max(3600,self.wall_seconds+1800)),metadata={"harbor_logs":str(self.logs_dir),"worker":meta})
        self.logs_dir.mkdir(parents=True,exist_ok=True)
        try:
            await environment.upload_file(str(self.worker),self.runtime+".tgz")
            q=shlex.quote
            result=await environment.exec(f"mkdir -p {q(self.runtime)} && tar -xzf {q(self.runtime+'.tgz')} -C {q(self.runtime)} && chmod -R a+rX {q(self.runtime)}",timeout_sec=180)
            if result.return_code!=0:raise RuntimeError("Worker extraction failed; no model request was dispatched")
            node=self.runtime+"/reverpi-worker/node/bin/node"
            health=await environment.exec(f"{q(node)} --version",timeout_sec=20)
            if health.return_code!=0:raise RuntimeError("Worker Node ABI/architecture is incompatible with this task environment")
            ledger=self._ledger()
            with ledger.db() as db:profile=json.loads(db.execute("SELECT value FROM meta WHERE key LIKE 'provider:%'").fetchone()[0])
            self.profile=profile
            with tempfile.TemporaryDirectory() as folder:
                path=Path(folder)/"settings.json";atomic_write(path,canonical(settings(profile["effort"],profile["max_output_tokens"],profile["context_window"])))
                made=await environment.exec(f"mkdir -p {q(self.runtime+'/agent-config')} {q(self.runtime+'/home')} {q(self.runtime+'/logs')}",timeout_sec=10)
                if made.return_code!=0:raise RuntimeError("Cannot create isolated Pi state")
                await environment.upload_file(str(path),self.runtime+"/agent-config/settings.json")
            atomic_write(self.logs_dir/"reverpi_setup.json",canonical({"session":self.sid,"method":self.method,"worker":meta,"status":"ready","pi_settings":settings(profile["effort"],profile["max_output_tokens"],profile["context_window"])}))
        except BaseException:
            self.sessions.disable(self.sid);raise

    async def run(self,instruction:str,environment:BaseEnvironment,context:AgentContext):
        if self.started or not self.token:raise RuntimeError("This trial cannot be replayed or run before setup")
        self.started=True
        q=shlex.quote;runtime=self.runtime
        status={"method":self.method,"task_instruction_sha":digest(instruction),"status":"running","official_reward":None}
        atomic_write(self.logs_dir/"reverpi_runtime.json",canonical(status))
        try:
            with tempfile.TemporaryDirectory() as folder:
                prompt=Path(folder)/"prompt.txt";atomic_write(prompt,instruction)
                await environment.upload_file(str(prompt),runtime+"/prompt.txt")
            worker=runtime+"/reverpi-worker"
            argv=command(worker+"/node/bin/node",worker+"/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js",
                         worker+"/pi/src/index.ts",runtime+"/logs/session.jsonl",runtime+"/prompt.txt",self.profile["model"],self.profile["effort"])
            env={"HOME":runtime+"/home","PI_CODING_AGENT_DIR":runtime+"/agent-config","PI_OFFLINE":"1",
                 "REVER_GATEWAY_URL":self.gateway_url,"REVER_SESSION_TOKEN":self.token,
                 "REVER_ALLOW_PRIVATE_HTTP":"1","REVER_MAX_TOOLS":str(self.max_tools),"REVER_MAX_TURNS":str(self.max_turns),
                 "PATH":worker+"/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin","NO_COLOR":"1"}
            # No verifier or gold is uploaded. Harbor's verifier remains the authority.
            result=await environment.exec("ulimit -f 204800; "+" ".join(map(q,argv))+f" >{q(runtime+'/logs/events.jsonl')} 2>{q(runtime+'/logs/stderr.log')}",
                                          env=env,timeout_sec=self.wall_seconds)
            status.update(status="process_exited",return_code=result.return_code)
        except BaseException as err:
            status.update(status="interrupted_or_failed",error_kind=type(err).__name__,possible_partial_tool_writes=True)
            raise
        finally:
            # Revoke new gateway requests BEFORE any potentially slow log copy.
            # This does not cancel already in-flight provider/tool work.
            self.sessions.disable(self.sid)
            # Preserve the original execution exception/reward. A timeout often leaves
            # useful session bytes behind; collection is best effort, never a rerun.
            try:
                await asyncio.wait_for(environment.download_dir(runtime+"/logs", str(self.logs_dir/"native")),
                                       timeout=self.LOG_COLLECTION_TIMEOUT_SECONDS)
                status["log_collection"] = {"status": "download_returned",
                    "session_file_present": (self.logs_dir/"native/session.jsonl").is_file()}
            except (Exception, asyncio.CancelledError) as collection_error:
                status["log_collection"] = {"status": "failed", "error_kind": type(collection_error).__name__}
                # New cancellation during cleanup must not make a successful run
                # appear uninterrupted. An already-propagating cancellation is kept.
                if isinstance(collection_error, asyncio.CancelledError) and status["status"] == "process_exited":
                    status["cleanup_cancelled"] = True
            ledger=self._ledger();totals=ledger.totals(self.sid)
            inp=out=cached=0
            counters_complete=True
            for a in ledger.attempts():
                if a["cell"]!=self.sid:continue
                parts=usage_parts(json.loads(a["raw_usage"])) if a["raw_usage"] else None
                if parts is None:
                    if a["actual_tokens"] != 0:counters_complete=False
                    continue
                i,o,c=parts;inp+=i;out+=o;cached+=c
            if totals["unknown_attempts"]==0 and counters_complete:
                context.n_input_tokens=inp;context.n_cache_tokens=cached;context.n_output_tokens=out
            context.cost_usd=totals["known_usd"] if totals["currency_is_fully_known"] and (self.profile["prices"]["configured"] or totals["attempts"] > 0) else None
            status.update(cost=totals,finished=time.time())
            atomic_write(self.logs_dir/"reverpi_runtime.json",canonical(status))
            if status.get("cleanup_cancelled"):
                raise asyncio.CancelledError()
