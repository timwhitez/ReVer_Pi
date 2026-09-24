#!/usr/bin/env python3
"""Render reproducible, descriptive figures; never query a model or provider."""
from pathlib import Path
import json,csv
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
ROOT=Path(__file__).resolve().parent
r=json.loads((ROOT/'results/resource_frontier.json').read_text())['rounds'][1]
out=ROOT/'paper/figures';out.mkdir(parents=True,exist_ok=True)
# Each figure is separate; default Matplotlib color cycle only.
fig,ax=plt.subplots(figsize=(6.5,3.2))
for arm in ('full','projected'):
    c=r['phases'][arm]['budget_curve']['curve']
    ax.step([v['k'] for v in c],[v['correct_by_k'] for v in c],where='post',label=arm)
ax.set(xlabel='Observed suffix request index k',ylabel='Correct final answer observed by k',xticks=list(range(1,9)),yticks=[0,1],ylim=(-.08,1.14))
ax.legend(loc='lower right');ax.set_title('One realized pair: request-to-answer trace')
fig.tight_layout();fig.savefig(out/'completion_trace.png',dpi=180);fig.savefig(out/'completion_trace.pdf');plt.close(fig)
fig,ax=plt.subplots(figsize=(6.5,3.2))
for p in r['resource_points']:
    ax.scatter([p['requests']],[p['tokens']],s=80,label=p['arm'])
    ax.annotate(f"{p['arm']}: {p['tokens']:,}",(p['requests'],p['tokens']),xytext=(10,3),textcoords='offset points')
ax.set(xlabel='Suffix requests through correct answer',ylabel='Logical task tokens (shared prefix included)',xlim=(2.7,5),xticks=[3,4,5],ylim=(75000,125000))
ax.set_title('Both observed points are nondominated');fig.tight_layout();fig.savefig(out/'resource_frontier.png',dpi=180);fig.savefig(out/'resource_frontier.pdf');plt.close(fig)
fig,ax=plt.subplots(figsize=(6.5,3.2))
xs=[i/200 for i in range(201)];coeff=r['price_coefficients']
for q in (0,5,20):
    ys=[(coeff['uncached']+coeff['cached']*x+coeff['output']*q)/1000 for x in xs]
    ax.plot(xs,ys,label=f'Output / uncached price = {q}')
ax.axhline(0,linestyle='--',linewidth=1)
ax.set(xlabel='Cached / uncached input price ratio',ylabel='Projected minus full (normalized thousands)',title='Hypothetical price sensitivity, not an invoice')
ax.legend();fig.tight_layout();fig.savefig(out/'price_sensitivity.png',dpi=180);fig.savefig(out/'price_sensitivity.pdf');plt.close(fig)
with (ROOT/'results/phase_usage.csv').open('w',newline='') as f:
    w=csv.writer(f);w.writerow(['round','phase','state','requests','input','cached_subset','output','total','correct_completion_cost_observed'])
    allr=json.loads((ROOT/'results/resource_frontier.json').read_text())['rounds']
    for rd in allr:
        for ph,v in rd['phases'].items():
            u=v['usage'];w.writerow([rd['round'],ph,v['status'],len(v['rows']),u['input_tokens'],u['cached_tokens'],u['output_tokens'],u['total_tokens'],v['tokens_to_correct_answer'] is not None])
print(json.dumps({'figures':3,'data':'results/resource_frontier.json','new_model_calls':0}))
