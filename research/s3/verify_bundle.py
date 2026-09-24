#!/usr/bin/env python3
"""Check the pristine S3 distribution; no network, install or live model calls.

Run before installing dependencies into this tree. Additional runtime files are
not silently accepted by this strict inventory checker.
"""
from pathlib import Path
import sys,json
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from reverpi_study.safeio import verify_tree

def main():
    root=Path(__file__).resolve().parents[2]
    result=verify_tree(root,'SHA256SUMS.json')
    print(json.dumps({'status':'passed','root':str(root),'scope':'file_integrity_not_signature_or_model_validation',**result}))
    return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,KeyError,TypeError) as e:
        print(type(e).__name__+': '+str(e),file=sys.stderr);raise SystemExit(2)
