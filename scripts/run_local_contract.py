#!/usr/bin/env python3
"""Run ONLY the fixed scripted native-compaction contract under a loopback guard."""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys
from verify_offline import clean_environment, validate_destination, Steps, atomic_report
ROOT=Path(__file__).resolve().parents[1]
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
 p.add_argument('--protocol',choices=['both','chat_completions','responses'],default='both')
 a=p.parse_args();out=validate_destination(ROOT,a.out);out.mkdir(parents=True,mode=0o700)
 (out/'logs').mkdir();(out/'runtime').mkdir();(out/'runtime/home').mkdir()
 cc=shutil.which('cc')
 if not sys.platform.startswith('linux') or not cc:raise SystemExit('Linux compiler/socket guard required; no unguarded fallback')
 guard=out/'runtime/guard.so'
 with (out/'logs/guard_compile.log').open('x') as f:
  subprocess.run([cc,'-shared','-fPIC','-O2','-Wall','-Wextra',str(ROOT/'scripts/offline_socket_guard.c'),'-o',str(guard),'-ldl'],stdout=f,stderr=subprocess.STDOUT,check=True,timeout=30)
 env=clean_environment(ROOT,out/'runtime/home',guard,out/'logs/nonloopback_blocks.log');steps=Steps(out,env)
 selftest=steps.run('guard_selftest',[sys.executable,'-c',"import socket,errno; s=socket.socket(); s.settimeout(.2)\ntry:s.connect(('203.0.113.1',443))\nexcept OSError as e:\n if e.errno!=errno.EACCES:raise\nelse:raise RuntimeError('guard absent')\nfinally:s.close()"],timeout=10)
 if selftest['status']!='passed':return 2
 step=steps.run('native_compaction',[sys.executable,'scripts/native_compaction_smoke.py','--out',str(out/'contract'),'--protocol',a.protocol],timeout=600)
 import json
 report=json.loads((out/'contract/report.json').read_text()) if (out/'contract/report.json').is_file() else {}
 log=out/'logs/nonloopback_blocks.log';blocks=len(log.read_text().splitlines()) if log.exists() else 0
 passed=step['status']=='passed' and report.get('status')=='passed' and blocks==1
 if passed:
  audit_step=steps.run('artifact_recheck',[sys.executable,'scripts/native_compaction_audit.py','--run',str(out/'contract'),'--out',str(out/'artifact_audit.json')],timeout=60)
  passed=audit_step['status']=='passed'
 result={'schema':1,'status':'passed' if passed else 'failed','paid_model_calls':0,
   'external_guard_blocks_including_selftest':blocks,'steps':steps.rows,'model_quality_certified':False,
   'supplier_billing_telemetry':False,'basis':'fixed in-process MockTransport; local Pi/gateway; no live CLI parameters'}
 atomic_report(out/'summary.json',result);print(json.dumps({'status':result['status'],'paid_model_calls':0}));return 0 if passed else 1
if __name__=='__main__':raise SystemExit(main())
