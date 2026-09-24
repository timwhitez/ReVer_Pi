#!/usr/bin/env python3
"""Controller-only development scorer; exact frozen gold required, never dispatched."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.intervention_scoring import score_matrix

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ('root','gold','out'):p.add_argument('--'+k,type=Path,required=True)
    p.add_argument('--plan-sha256',required=True);a=p.parse_args()
    if a.out.exists():raise ValueError('Refusing to replace an earlier evaluation')
    report=score_matrix(a.root,a.plan_sha256,a.gold)
    a.out.parent.mkdir(parents=True,exist_ok=True)
    with a.out.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    a.out.chmod(0o600)
    print(json.dumps({'output':str(a.out),'formal_gate_passed':False,'population_effect_established':False}))
if __name__=='__main__':main()
