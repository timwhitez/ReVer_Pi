"""Development-only Pareto screening; not a test of noninferiority or novelty."""
from __future__ import annotations
import json
from pathlib import Path
from .statistics import analyze
from .errors import LabError
from .util import digest,canonical,atomic_write


def select_development(run:Path,out:Path,baseline:str,quality_tolerance:float=0.0):
    if not 0<=quality_tolerance<=0.2:raise ValueError('Declare a quality tolerance in [0,0.2]')
    manifest=json.loads((run/'manifest.json').read_text())
    if manifest['study']['split'] not in {'search','dev'}:
        raise LabError('heldout_leakage','Gate/external outcomes must not be fed back into candidate selection')
    from .results import read_results,current_cost_view
    rows,cost_source=current_cost_view(run,manifest,read_results(run,manifest));methods=manifest['study']['methods']
    report=analyze(rows,methods,baseline=baseline)
    base={r['task_id']:r for r in rows if r['repeat']==0 and r['method']==baseline}
    candidates=[]
    for method in methods:
        if method==baseline:continue
        chosen={r['task_id']:r for r in rows if r['repeat']==0 and r['method']==method}
        pairable=set(chosen)==set(base) and bool(base)
        all_observed=pairable and all(r.get('success') is not None and r.get('cost',{}).get('unknown_attempts',1)==0 for r in [*base.values(),*chosen.values()])
        info={'method':method,'eligible_for_development_screen':False,'complete_same_task_set':all_observed}
        if all_observed:
            n=len(base);delta=sum(int(chosen[k]['success'])-int(base[k]['success']) for k in base)/n
            tokens=sum(r['cost']['accounted_tokens'] for r in chosen.values());base_tokens=sum(r['cost']['accounted_tokens'] for r in base.values())
            info.update(quality_delta=delta,success_rate=sum(int(r['success']) for r in chosen.values())/n,tokens=tokens,
                token_reduction=1-tokens/base_tokens if base_tokens else None,
                eligible_for_development_screen=delta>=-quality_tolerance and tokens<base_tokens)
        candidates.append(info)
    eligible=[r for r in candidates if r['eligible_for_development_screen']]
    pareto=[r['method'] for r in eligible if not any(s['success_rate']>=r['success_rate'] and s['tokens']<=r['tokens'] and (s['success_rate']>r['success_rate'] or s['tokens']<r['tokens']) for s in eligible)]
    output={'mock':manifest['mock'],'source_run':str(run.resolve()),'results_sha':digest(rows),'manifest_sha':digest(manifest),
        'quality_tolerance':quality_tolerance,'candidates':candidates,'cost_source':cost_source,'pareto_development_candidates':pareto,
        'automatically_freezes_final_candidate':False,'proves_noninferiority':False,
        'warning':'Developer screen only. Small correlated tasks and adaptive candidate search do not establish final generalization. Freeze one candidate before Gate/External.',
        'statistics':report}
    if out.exists():raise ValueError('Selection output exists; retain each decision generation')
    atomic_write(out,canonical(output));return output
