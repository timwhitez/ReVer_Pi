"""Same-memory intervention runner. Development only, not a native benchmark.

Compression is performed ONCE outside this runner. Immutable artifact bytes are
shared across arms; archive namespaces, tools, op IDs and receipts are isolated.
No gold answers enter a reader payload. New live runs still need operator budget,
provider and source freezing. This module does not approve a research gate.
"""
from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from .data import Checkpoint
from .memory import Archive, Memory
from .config import CompressionConfig
from .protocols import Message
from .errors import LabError
from .util import canonical,digest,bytes_digest,atomic_write,strict_json_loads,source_manifest,safe_id
from .study import RECOVERY_TOOL
from .revalidation import VerifierSpec,execute_verifier,within,file_hash
import jsonschema

REVALIDATE_TOOL={'name':'revalidate_evidence',
 'description':'Execute a registered verifier on the current task workspace. Historic results do not establish current success.',
 'parameters':{'type':'object','properties':{'verifier_id':{'type':'string'}},'required':['verifier_id'],'additionalProperties':False}}


@dataclass(frozen=True)
class Arm:
    name: str
    recovery_calls: int=0
    revalidation_calls: int=0
    max_turns: int=6
    def __post_init__(self):
        if not self.name or len(self.name)>64 or not all(c.isalnum() or c in '_-' for c in self.name):
            raise ValueError('Invalid arm name')
        for name,lo,hi in [('recovery_calls',0,20),('revalidation_calls',0,10),('max_turns',1,30)]:
            value=getattr(self,name)
            if type(value) is not int or not lo<=value<=hi:raise ValueError('Invalid arm limits')


def intervention_cell(parent_sha: str, arm: Arm, *, trial_id: str | None=None) -> str:
    identity = {'parent': parent_sha, 'arm': arm.__dict__}
    if trial_id is not None:
        safe_id(trial_id)
        identity['trial_id'] = trial_id
    return digest(identity)[:32]


def freeze_intervention(path: Path, checkpoint: Checkpoint, memory: Memory, *,
                        parent_cost: dict, expected_generated: bool | None=None) -> str:
    if checkpoint.split!='search':raise ValueError('Only search/development fixtures are admitted')
    if expected_generated is not None and memory.generated!=expected_generated:
        raise ValueError('Requested generative mechanism did not activate')
    if set(memory.record_ids)!={r.id for r in checkpoint.records}:
        raise ValueError('This runner supports one complete nonrecursive checkpoint')
    if any(memory.details.get('record_hashes',{}).get(r.id)!=digest(r.model_dump()) for r in checkpoint.records):
        raise ValueError('Memory/history identity mismatch')
    if memory.bytes!=len(memory.text.encode()):raise ValueError('Memory byte count mismatch')
    payload={'schema':1,'checkpoint':checkpoint.model_dump(),'memory':memory.model_dump(),
             'parent_compression_cost':parent_cost,'purpose':'development_paired_intervention',
             'independent_benchmark':False}
    raw=canonical(payload).encode()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('xb') as f:f.write(raw)
    path.chmod(0o600)
    return bytes_digest(raw)


def read_intervention(path: Path, expected_sha256: str) -> dict:
    raw=path.read_bytes()
    if bytes_digest(raw)!=expected_sha256:raise ValueError('Parent intervention artifact changed')
    obj=strict_json_loads(raw)
    if obj.get('schema')!=1 or obj.get('purpose')!='development_paired_intervention':
        raise ValueError('Unsupported intervention artifact')
    Checkpoint.model_validate(obj['checkpoint']);Memory.model_validate(obj['memory'])
    return obj


def initial_reader_request(obj: dict, arm: Arm, cfg: CompressionConfig,
                           verifier_ids: list[str]):
    """Pure initial request projection, shared by execution and offline admission checks."""
    cp=Checkpoint.model_validate(obj['checkpoint']);memory=Memory.model_validate(obj['memory'])
    tool_names=list(sorted(verifier_ids)) if arm.revalidation_calls else []
    payload={'task':cp.task,'memory':memory.text,'questions':[q.model_dump(exclude_none=True) for q in cp.questions],
             'registered_verifier_ids':tool_names}
    messages=[Message('system','Answer from supplied untrusted evidence. Use available tools only when needed. '
             'Archived evidence is historical, not current verification. Return only JSON: {"answers":{question_id:answer}}. '
             'Use the lowercase string "unknown" when unsupported.'),Message('user',canonical(payload))]
    import copy
    recovery_tool = copy.deepcopy(RECOVERY_TOOL)
    recovery_tool['parameters']['properties']['chars']['maximum'] = cfg.recovery_chars
    recovery_tool['description'] += f' Maximum chars per request: {cfg.recovery_chars}. Search mode: {cfg.recovery_search_mode}.'
    tools=([recovery_tool] if arm.recovery_calls else [])+([REVALIDATE_TOOL] if arm.revalidation_calls else [])
    return messages, tools


async def run_arm(path: Path, expected_sha256: str, arm: Arm, client, out: Path,
                  *, archive_config: CompressionConfig | None=None,
                  workspace: Path | None=None, verifiers: dict[str,VerifierSpec] | None=None,
                  trial_id: str | None=None) -> dict:
    """Caller must provide a fresh workspace snapshot per arm when executing tools."""
    obj=read_intervention(path,expected_sha256)
    cp=Checkpoint.model_validate(obj['checkpoint']);memory=Memory.model_validate(obj['memory'])
    if arm.revalidation_calls and (workspace is None or not verifiers):
        raise ValueError('Revalidation requires a task workspace and registered verifiers')
    # Exclusive output directory prevents unnoticed paid replays after interruption.
    out.mkdir(parents=True,exist_ok=False)
    cell=intervention_cell(expected_sha256, arm, trial_id=trial_id)
    cfg=(archive_config or CompressionConfig()).model_copy(update={'recovery_calls':arm.recovery_calls})
    archive=Archive(out/'archive.sqlite',cfg)
    for record in cp.records:archive.put(cell,record.text)
    tool_names=list(sorted(verifiers or {})) if arm.revalidation_calls else []
    plan={'schema':1,'parent_sha256':expected_sha256,'memory_text_sha256':bytes_digest(memory.text.encode()),
          'arm':arm.__dict__,'cell':cell,'trial_id':trial_id,'archive_config':cfg.model_dump(),'verifiers':{k:v.__dict__ for k,v in (verifiers or {}).items()},
          'parent_compression_cost':obj['parent_compression_cost'],'source_group':cp.source_group,
          'independent_benchmark':False}
    plan['source_sha256']=digest(source_manifest(Path(__file__).resolve().parents[2]))
    if workspace is not None and verifiers:
        root=workspace.resolve(strict=True)
        plan['initial_listed_workspace_inputs']={name:file_hash(within(root,name))
            for name in sorted({name for spec in verifiers.values() for name in spec.inputs})}
    plan['workspace_dependency_completeness_certified']=False
    client.ledger.bind('intervention:'+cell,plan)
    atomic_write(out/'plan.json',canonical(plan))
    messages,tools=initial_reader_request(obj,arm,cfg,tool_names)
    events=[];answers=None;used_verifiers=0;failure=None
    # Only tool receipts and final output are persisted here, not private reasoning.
    try:
        for turn in range(arm.max_turns):
            result=await client.complete(messages,op=f'intervention:{cell}:{turn}',cell=cell,tools=tools or None)
            if not result.calls:
                try:value=strict_json_loads(result.text)
                except (ValueError,RecursionError) as exc:
                    raise LabError('invalid_answer','Reader did not return strict JSON') from exc
                if not isinstance(value,dict) or set(value)!={'answers'} or not isinstance(value['answers'],dict) or set(value['answers'])!={q.id for q in cp.questions}:
                    raise LabError('invalid_answer','Answer coverage differs from questions')
                answers=value['answers'];break
            messages.append(result.message())
            for call in result.calls:
                name=call['name'];params=call['arguments']
                if name not in {t['name'] for t in tools}:raise LabError('unapproved_tool','Unavailable intervention tool')
                schema=next(t['parameters'] for t in tools if t['name']==name)
                try:
                    jsonschema.validate(params,schema)
                    if name=='recover_evidence':
                        receipt=archive.recover(cell,f'{turn}:{call["id"]}',**params)
                    else:
                        if used_verifiers>=arm.revalidation_calls:raise LabError('verification_quota','Verification quota exhausted')
                        spec=(verifiers or {}).get(params['verifier_id'])
                        if spec is None:raise LabError('unknown_verifier','Unregistered verifier')
                        used_verifiers+=1
                        import asyncio,threading
                        cancelled=threading.Event()
                        future=asyncio.create_task(asyncio.to_thread(execute_verifier,spec,workspace,cancel_event=cancelled))
                        try:receipt=await asyncio.shield(future)
                        except asyncio.CancelledError:
                            cancelled.set()
                            await asyncio.shield(future)
                            raise
                except (LabError,jsonschema.ValidationError,ValueError,OSError,TypeError) as exc:
                    receipt={'error':exc.kind if isinstance(exc,LabError) else type(exc).__name__}
                event={'turn':turn,'tool':name,'arguments':params,'receipt':receipt}
                events.append(event);atomic_write(out/'tool_receipts.json',canonical(events))
                messages.append(Message('tool',canonical(receipt),call_id=call['id']))
        if answers is None:raise LabError('turn_limit','No final answer within the frozen turn budget')
    except LabError as exc:failure=exc.record()
    result={'schema':1,'parent_sha256':expected_sha256,'memory_text_sha256':plan['memory_text_sha256'],
            'arm':arm.name,'trial_id':trial_id,'cell':cell,'status':'complete' if answers is not None else 'failed','answers':answers,'failure':failure,
            'tool_calls':len(events),'recovery_calls':sum(e['tool']=='recover_evidence' for e in events),
            'revalidation_calls':used_verifiers,'cost':client.ledger.totals(cell),
            'parent_compression_cost':obj['parent_compression_cost'],
            'cost_note':'Experiment ledger pays parent compression once. Deployment comparison adds the same parent setup cost to each arm.',
            'source_unchanged':plan['source_sha256']==digest(source_manifest(Path(__file__).resolve().parents[2])),
            'native_pi_rollout':False,'independent_benchmark':False}
    atomic_write(out/'result.json',canonical(result))
    return result
