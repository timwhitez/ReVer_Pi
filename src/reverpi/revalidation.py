"""Bounded, operator-registered verifier execution in the TASK workspace.

This is not a sandbox: execute only inside an authorized disposable task
container. A model supplies a verifier ID, never a shell command. Receipts do
not certify completeness of dependencies or sufficiency for the task.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
import os
import selectors
import signal
import subprocess
import sys
import time
import threading
from dataclasses import dataclass
from pathlib import Path


def canonical(value) -> bytes:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()


def file_hash(path: Path) -> str:
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def within(root: Path, relative: str) -> Path:
    p = Path(relative)
    if not relative or p.is_absolute() or '..' in p.parts or p == Path('.'):
        raise ValueError('Input path must be a nonempty relative file path')
    target = root / p
    # Do not follow mutable symlink inputs or escape a task workspace.
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Symlink dependency rejected')
    target = target.resolve(strict=True)
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError('Input is not a workspace file')
    return target


@dataclass(frozen=True)
class VerifierSpec:
    id: str
    argv: tuple[str,...]
    inputs: tuple[str,...]
    timeout_seconds: float = 30
    max_output_bytes: int = 32768

    def __post_init__(self):
        if not self.id or len(self.id)>128 or not all(c.isalnum() or c in '_-.' for c in self.id):
            raise ValueError('Invalid registered verifier ID')
        if not isinstance(self.argv,tuple) or not self.argv or not all(isinstance(a,str) and a and '\0' not in a for a in self.argv):
            raise ValueError('argv must be a nonempty tuple of nonempty strings')
        if (not isinstance(self.inputs,tuple) or not self.inputs
                or not all(isinstance(x,str) and x and '\0' not in x for x in self.inputs)
                or len(set(self.inputs)) != len(self.inputs)):
            raise ValueError('Unique explicit input files are required')
        if type(self.timeout_seconds) not in {int,float} or not math.isfinite(self.timeout_seconds) or not 0<self.timeout_seconds<=600:
            raise ValueError('Timeout outside supported bounds')
        if type(self.max_output_bytes) is not int or not 1<=self.max_output_bytes<=1048576:
            raise ValueError('Output cap outside supported bounds')


def load_registry(path: Path, expected_sha256: str) -> dict[str,VerifierSpec]:
    raw = path.read_bytes()
    if len(raw)>1048576 or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('Registry identity mismatch')
    def strict_object(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise ValueError('Duplicate registry key')
            result[key]=value
        return result
    obj=json.loads(raw,object_pairs_hook=strict_object)
    if (not isinstance(obj,dict) or set(obj)!={'schema','verifiers'}
            or type(obj['schema']) is not int or obj['schema']!=1
            or not isinstance(obj['verifiers'],list)):
        raise ValueError('Invalid verifier registry')
    result={}
    for row in obj['verifiers']:
        if not isinstance(row,dict) or set(row)-{'id','argv','inputs','timeout_seconds','max_output_bytes'}:
            raise ValueError('Invalid verifier specification')
        if not isinstance(row.get('argv'),list) or not isinstance(row.get('inputs'),list):raise ValueError('argv/inputs must be arrays')
        spec=VerifierSpec(**{**row,'argv':tuple(row['argv']),'inputs':tuple(row['inputs'])})
        if spec.id in result:raise ValueError('Duplicate verifier ID')
        result[spec.id]=spec
    return result


def _kill(proc: subprocess.Popen) -> None:
    # Even if the parent exited, descendants might still hold output pipes open.
    try:os.killpg(proc.pid,signal.SIGKILL)
    except ProcessLookupError:pass


def execute_verifier(spec: VerifierSpec, workspace: Path, *, cancel_event: threading.Event | None=None,
                     invocation_nonce: str | None=None) -> dict:
    if invocation_nonce is not None and not re.fullmatch(r'[a-f0-9]{64}', invocation_nonce):
        raise ValueError('Invalid invocation nonce')
    if os.name!='posix':raise ValueError('POSIX process-group containment required')
    if cancel_event is not None and cancel_event.is_set():raise ValueError('Execution cancelled before dispatch')
    root=workspace.resolve(strict=True)
    if not root.is_dir():raise ValueError('Workspace must be a directory')
    paths={name:within(root,name) for name in spec.inputs}
    before={name:file_hash(p) for name,p in paths.items()}
    safe_path=os.pathsep.join(p for p in os.defpath.split(os.pathsep) if p and Path(p).is_absolute())
    if not safe_path:raise ValueError('No absolute system PATH for registered verifier')
    env={'PATH':safe_path,'LANG':'C.UTF-8','LC_ALL':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1'}
    # No provider keys, tokens, proxy variables or user HOME are inherited.
    started=time.monotonic();chunks=[];count=0;reason='completed';proc=None
    try:
        proc=subprocess.Popen(list(spec.argv),cwd=root,env=env,stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,shell=False,start_new_session=True)
        assert proc.stdout is not None
        with selectors.DefaultSelector() as sel:
            os.set_blocking(proc.stdout.fileno(),False);sel.register(proc.stdout,selectors.EVENT_READ)
            while sel.get_map():
                if cancel_event is not None and cancel_event.is_set():
                    reason='cancelled';_kill(proc);break
                remaining=spec.timeout_seconds-(time.monotonic()-started)
                if remaining<=0:
                    reason='timeout';_kill(proc);break
                for key,_ in sel.select(min(remaining,.05)):
                    block=os.read(key.fileobj.fileno(),min(65536,spec.max_output_bytes-count+1))
                    if not block:sel.unregister(key.fileobj);continue
                    room=spec.max_output_bytes-count;chunks.append(block[:room]);count+=len(block)
                    if count>spec.max_output_bytes:reason='output_limit';_kill(proc);break
                if reason!='completed':break
            remaining=spec.timeout_seconds-(time.monotonic()-started)
            if reason=='completed':
                try:proc.wait(timeout=max(.001,remaining))
                except subprocess.TimeoutExpired:reason='timeout';_kill(proc)
        if proc.poll() is None:_kill(proc)
        proc.wait(timeout=5)
    finally:
        if proc is not None:
            _kill(proc)
            if proc.poll() is None:proc.wait(timeout=5)
            if proc.stdout:proc.stdout.close()
    after={}
    for name in paths:
        try:after[name]=file_hash(within(root,name))
        except (OSError,ValueError):after[name]=None
    stable=before==after
    data=b''.join(chunks)
    return {'schema':'reverpi.verification-receipt.v1','verifier_id':spec.id,
        'invocation_nonce':invocation_nonce,
        'workspace_path_sha256':hashlib.sha256(str(root).encode('utf-8')).hexdigest(),
        'receipt_is_cryptographic_attestation':False,
        'verifier_spec_sha256':hashlib.sha256(canonical(spec.__dict__)).hexdigest(),
        'argv':list(spec.argv),'status':reason,'returncode':proc.returncode,
        'passed':reason=='completed' and stable and proc.returncode==0,
        'input_sha256_before':before,'input_sha256_after':after,'observed_inputs_stable':stable,
        'output':data.decode('utf-8',errors='replace'),'output_sha256':hashlib.sha256(data).hexdigest(),
        'output_truncated':reason=='output_limit','elapsed_seconds':time.monotonic()-started,
        'dependency_completeness_certified':False,'decision_sufficiency_certified':False,
        'sandbox_provided_by_this_module':False}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--registry',type=Path,required=True)
    ap.add_argument('--registry-sha256',required=True)
    ap.add_argument('--workspace',type=Path,required=True)
    ap.add_argument('--verifier-id',required=True)
    ap.add_argument('--invocation-nonce')
    args=ap.parse_args()
    try:
        registry=load_registry(args.registry,args.registry_sha256)
        if args.verifier_id not in registry:raise ValueError('Unregistered verifier ID')
        cancelled=threading.Event()
        signal.signal(signal.SIGTERM,lambda *_:cancelled.set())
        signal.signal(signal.SIGINT,lambda *_:cancelled.set())
        result=execute_verifier(registry[args.verifier_id],args.workspace,cancel_event=cancelled,invocation_nonce=args.invocation_nonce)
        print(canonical(result).decode())
    except Exception as exc:
        # Arguments, filesystem names, and subprocess exception strings may be private.
        print(json.dumps({'error':'verifier_execution_error','class':type(exc).__name__}))
        raise SystemExit(2)

if __name__=='__main__':main()
