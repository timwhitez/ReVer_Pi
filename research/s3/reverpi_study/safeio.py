"""Read-only integrity helpers for cooperative, stationary local evidence.

Reject links and nonregular files. These are not an adversarial-owner signature
or an atomic filesystem snapshot. No HTTP clients or model operations exist here.
"""
from __future__ import annotations
import hashlib,json,os,stat
from pathlib import Path, PurePosixPath

MAX_JSON_BYTES=64*1024*1024

def require(ok:bool, message:str)->None:
    if not ok:raise ValueError(message)

def parse(data: str|bytes):
    def unique(rows):
        out={}
        for k,v in rows:
            require(k not in out,'Duplicate JSON key: '+k);out[k]=v
        return out
    def bad(_):raise ValueError('Nonfinite JSON constant')
    return json.loads(data,object_pairs_hook=unique,parse_constant=bad)

def canonical(value)->bytes:
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf8')

def digest(value)->str:return hashlib.sha256(canonical(value)).hexdigest()

def regular(path:Path)->Path:
    p=path.absolute()
    for a in [p,*p.parents]:
        require(not a.is_symlink(),'Symbolic links are not evidence inputs')
    require(stat.S_ISREG(p.stat().st_mode),'Expected a regular file')
    return p

def load(path:Path):
    p=regular(path);require(p.stat().st_size<=MAX_JSON_BYTES,'JSON exceeds size limit')
    return parse(p.read_bytes())

def filehash(path:Path)->str:
    with regular(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def relative_name(s:str)->str:
    require(type(s) is str and s and '\\' not in s and '\x00' not in s,'Invalid path')
    p=PurePosixPath(s)
    require(not p.is_absolute() and '..' not in p.parts and '.' not in s.split('/') and str(p)==s,'Unsafe or noncanonical path')
    return s

def verify_tree(root:Path, manifest_name:str)->dict:
    relative_name(manifest_name)
    require(root.is_dir() and not root.is_symlink(),'Missing regular evidence root')
    for a in root.absolute().parents:require(not a.is_symlink(),'Unsafe root ancestor')
    entries=load(root/manifest_name)['files']
    require(type(entries) is dict,'Manifest files must be a mapping')
    for k,v in entries.items():
        relative_name(k)
        require(type(v) is str and len(v)==64 and all(c in '0123456789abcdef' for c in v),'Invalid SHA256')
    actual={}
    for p in root.rglob('*'):
        require(not p.is_symlink(),'Symlink in evidence tree')
        if p.is_dir():continue
        k=p.relative_to(root).as_posix();actual[k]=filehash(p)
    require(set(actual)==set(entries)|{manifest_name},'Manifest inventory mismatch')
    require(all(actual[k]==v for k,v in entries.items()),'Manifest content mismatch')
    return {'verified_files':len(entries),'all_files':len(actual),'manifest_sha256':actual[manifest_name]}

def exclusive_json(path:Path,value)->None:
    p=path.absolute()
    for a in [p,*p.parents]:require(not a.is_symlink(),'Unsafe output ancestor')
    require(not p.exists(),'Output exists; choose a fresh path')
    payload=canonical(value)+b'\n'
    p.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create is sufficient for the stated cooperative local threat model.
    with p.open('xb') as f:f.write(payload)
