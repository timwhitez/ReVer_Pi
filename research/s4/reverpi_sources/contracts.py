"""Strict public snapshots, task identities and controller-only answer scoring.

Hashes detect accidental or partial drift, not coordinated malicious rewriting.
The local actor allowlist is an experiment guard, not a kernel sandbox.
"""
from __future__ import annotations
import hashlib,json,os,re,stat
from pathlib import Path
from typing import Literal
from pydantic import Field,model_validator
from reverpi.paired_prefix import PairedPlan,require
from reverpi.util import canonical,digest,strict_json_loads,atomic_create,source_manifest

S4=Path(__file__).resolve().parents[1]
ROOT=S4.parents[1]
MAX_FILE=1024*1024
MAX_SNAPSHOT=8*1024*1024

def relative(name):
    require(type(name) is str and 0<len(name)<=300 and re.fullmatch(r'[A-Za-z0-9_.\-/]+',name) is not None,'Noncanonical relative path')
    require(not name.startswith('/') and all(p not in {'','.','..'} for p in name.split('/')),'Path escape or alias')
    return name

def read_file(root:Path,name:str,maximum=MAX_FILE)->bytes:
    """Open each directory component with NOFOLLOW; reject streams and devices."""
    parts=relative(name).split('/')
    require(root.is_dir() and all(not a.is_symlink() for a in [root,*root.parents]),'Missing regular snapshot root or symlink ancestor')
    fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            nxt=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=nxt
        f=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            st=os.fstat(f);require(stat.S_ISREG(st.st_mode) and st.st_size<=maximum,'Nonregular or oversized file')
            with os.fdopen(f,'rb',closefd=False) as h:data=h.read(maximum+1)
            require(len(data)<=maximum,'File grew beyond bound');return data
        finally:os.close(f)
    finally:os.close(fd)

def tree_manifest(root:Path)->dict[str,str]:
    require(root.is_dir() and not root.is_symlink(),'Snapshot is not a regular directory')
    result={};total=0
    for here,dirs,files in os.walk(root,followlinks=False):
        for d in dirs:require(not (Path(here)/d).is_symlink(),'Symlink directory')
        for name in sorted(files):
            p=(Path(here)/name).relative_to(root).as_posix();data=read_file(root,p)
            total+=len(data);require(total<=MAX_SNAPSHOT and len(result)<1000,'Snapshot bounds exceeded')
            result[p]=hashlib.sha256(data).hexdigest()
    require(bool(result),'Empty source snapshot');return dict(sorted(result.items()))

def research_manifest():
    return {p.relative_to(S4).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(S4.rglob('*')) if p.is_file() and not p.is_symlink()
            and p.suffix in {'.py','.ts','.mjs'} and p.relative_to(S4).parts[0] not in {'data','validation','results','paper'}}

def load_task(path:Path)->dict:
    value=strict_json_loads(read_file(path.parent,path.name));require(type(value) is dict,'Task object required')
    expected={'schema','task_id','source_id','source_group','split','inspected','origin','official_benchmark','snapshot_sha256','files','prompt','answer_keys','artifact_version','replay_scope','projected_answer_hidden_requirement','task_sha256'}
    require(set(value)==expected,'Task fields changed')
    copy=dict(value);h=copy.pop('task_sha256');require(digest(copy)==h,'Task digest mismatch')
    require(value['schema']=='reverpi.s4.task.v1' and value['split']=='development' and value['inspected'] is True,'This adapter handles inspected development tasks only')
    require(value['official_benchmark'] is False and value['replay_scope']=='readonly_snapshot','Incorrect benchmark/snapshot claim')
    relative(value['task_id']);relative(value['source_id']);require('/' not in value['task_id']+value['source_id'],'Identifier is not a path')
    require(type(value['files']) is dict and 0<len(value['files'])<=1000,'Invalid file inventory')
    for name,h in value['files'].items():
        relative(name);require(type(h) is str and re.fullmatch('[a-f0-9]{64}',h) is not None,'Invalid file digest')
    require(digest(value['files'])==value['snapshot_sha256'],'Snapshot identity mismatch')
    require(type(value['prompt']) is str and 0<len(value['prompt'])<=12000,'Invalid task prompt')
    keys=value['answer_keys'];require(type(keys) is list and keys==sorted(set(keys)) and keys and all(type(k) is str and re.fullmatch('[a-z_]+',k) for k in keys),'Invalid answer keys')
    return value

def render_prompt(task):
    return ('This is an inspected development question on a frozen installed-package source snapshot, not an official benchmark. '
            'The workspace is read-only. The available relative source paths are:\n'+'\n'.join(task['files'])+
            '\nAnswer by consulting source as needed; do not fabricate extra reads to lengthen a trajectory. '
            'You may finish without archive tools. Use JSON null for any field you cannot determine.\n\n'+task['prompt'])

class SourcePlan(PairedPlan):
    schema_version:Literal['reverpi.s4.source-pair.v1']='reverpi.s4.source-pair.v1'
    scope:Literal['inspected_development_readonly_source_pair']='inspected_development_readonly_source_pair'
    research_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    task_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    snapshot_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    gold_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    task_id:str=Field(pattern=r'^[a-z][a-z0-9-]{1,80}$')
    source_group:str=Field(min_length=2,max_length=200)
    scripted_profile:Literal['none','quick','walk']='none'
    authorization_digest:str|None=None
    @model_validator(mode='after')
    def source_checks(self):
        require(self.policy.recovery_interface=='split_v1','S4 freezes split_v1 for both arms')
        require(self.provider.mock==(self.scripted_profile!='none'),'Scripted actor/live route mismatch')
        require(not self.paid_authorized or (isinstance(self.authorization_digest,str) and re.fullmatch('[a-f0-9]{64}',self.authorization_digest)),'Paid plan requires a separately supplied authorization digest')
        return self

def verify_identity(plan,task):
    require(plan.source_sha256==digest(source_manifest(ROOT)),'Frozen agent source drift')
    require(plan.research_sha256==digest(research_manifest()),'Research executor drift')
    require(plan.task_sha256==task['task_sha256'] and plan.snapshot_sha256==task['snapshot_sha256'] and plan.task_id==task['task_id'] and plan.source_group==task['source_group'],'Task/plan identity mismatch')

def read_gold(path:Path,task,expected_sha):
    raw=read_file(path.parent,path.name);require(hashlib.sha256(raw).hexdigest()==expected_sha,'Gold file digest mismatch')
    g=strict_json_loads(raw);require(type(g) is dict,'Gold must be an object');require(g.get('schema')=='reverpi.s4.gold.v1' and g.get('task_sha256')==task['task_sha256'],'Wrong gold contract')
    require(type(g.get('expected')) is dict and sorted(g['expected'])==task['answer_keys'],'Gold keys differ from public task')
    return g

def score_answer(answer,task,gold):
    """Only exact typed JSON values; bool/int equality is not accepted as equivalence."""
    if answer is None:return {'status':'no_final_answer','correct':False,'fields':None}
    if type(answer) is not str or len(answer)>100000:return {'status':'invalid_answer','correct':False,'fields':None}
    try: obj=strict_json_loads(answer)
    except (ValueError,TypeError,RecursionError):return {'status':'invalid_json','correct':False,'fields':None}
    if type(obj) is not dict or sorted(obj)!=task['answer_keys']:return {'status':'schema_mismatch','correct':False,'fields':None}
    fields={k:canonical(obj[k])==canonical(gold['expected'][k]) for k in task['answer_keys']}
    return {'status':'scored','correct':all(fields.values()),'fields':fields,'unknown_fields':[k for k,v in obj.items() if v is None]}

def new_output(path:Path):
    p=path.absolute();require(not p.exists() and not p.is_symlink(),'Output must not exist')
    require(all(not a.is_symlink() for a in p.parents),'Output ancestor symlink')
    p=p.resolve();require(not p.is_relative_to(ROOT) and not ROOT.is_relative_to(p),'Output must be outside source tree')
    return p
