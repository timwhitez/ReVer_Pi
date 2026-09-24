#!/usr/bin/env python3
"""S5 offline-only assessment. No live execution or budget authorization."""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from research.s5.contracts import strict_loads, canonical, require
from research.s5.census import safe_read, audit_campaign, write_outputs
from research.s5.assurance import inspect_legacy_gate, assess

def read(path):
    p=Path(path).absolute();return strict_loads(safe_read(p.parent,p.name))

def emit(path,value):
    p=Path(path).absolute();data=canonical(value).encode()+b'\n'
    require(not p.is_symlink() and all(not x.is_symlink() for x in p.parents),'output_symlink')
    p.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'wb',closefd=False) as f:f.write(data);f.flush();os.fsync(f.fileno())
    finally:os.close(fd)

def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='cmd',required=True)
    c=sub.add_parser('census');c.add_argument('--delivery',type=Path,required=True);c.add_argument('--out',type=Path,required=True)
    i=sub.add_parser('inspect-legacy');i.add_argument('--candidate',required=True);i.add_argument('--calibration',required=True);i.add_argument('--authorization',required=True);i.add_argument('--out',required=True)
    a=sub.add_parser('assess');a.add_argument('--candidate',required=True);a.add_argument('--design',required=True);a.add_argument('--design-sha256',required=True);a.add_argument('--authorization',required=True);a.add_argument('--authorization-sha256',required=True);a.add_argument('--observations',required=True);a.add_argument('--out',required=True)
    args=p.parse_args()
    if args.cmd=='census':
        require(not args.out.exists(),'output_exists')
        value=audit_campaign(args.delivery,ROOT);write_outputs(value,args.out);print(canonical(value['totals']));return int(bool(value['errors']))
    if args.cmd=='inspect-legacy':value=inspect_legacy_gate(read(args.candidate),read(args.calibration),read(args.authorization))
    else:value=assess(read(args.candidate),read(args.design),read(args.authorization),read(args.observations),expected_design_sha256=args.design_sha256,expected_authorization_sha256=args.authorization_sha256)
    emit(args.out,value);print(canonical({'allow_projection':value['allow_projection'],'reasons':value['reasons'],'new_paid_authorization':False}))
    return 0 if value['allow_projection'] else 2

if __name__=='__main__':raise SystemExit(main())
