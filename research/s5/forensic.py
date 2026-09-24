"""Classify denied/pending read intents without relabelling them as safe successes."""
from __future__ import annotations
from .contracts import require

def read_intents(calls: dict, results: dict, allowed: set[str]) -> dict:
    denied=[];pending=[]
    for ident,call in calls.items():
        if call.get('name')!='read' or call.get('arguments',{}).get('path') in allowed:continue
        result=results.get(ident)
        if result is None:pending.append(ident);continue
        # An is_error flag alone is insufficient: an error can leak file bytes.
        require(result.get('is_error') is True and result.get('text')=='Operation aborted',
                'out_of_allowlist_result_not_proven_denied')
        denied.append(ident)
    return {'attempted_out_of_allowlist':len(denied)+len(pending),'denied_results':len(denied),
            'pending_without_result':len(pending),'no_successful_out_of_allowlist_result_observed':True,
            'all_read_intents_within_contract':not(denied or pending),
            'sandbox_certified':False}
