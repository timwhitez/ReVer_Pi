"""Outcome-blind benchmark subset planning and bounded descriptive reporting.

Reads metadata only: never task instructions, solutions, test bodies or answers.
A subset is not an official full-benchmark score. Smaller evaluation does NOT
preserve inferential precision; repeated source clusters do not increase n.
"""
from __future__ import annotations
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

from .risk_calibration import harm_upper_bound
from .util import atomic_write, canonical, digest, strict_json_loads


def _hex(value: object, n: int = 64) -> bool:
    return isinstance(value, str) and len(value) == n and all(c in '0123456789abcdef' for c in value)


def freeze_subset(inventory: dict, out: Path, *, n: int = 24, seed: int = 20260919) -> dict:
    """Proportional stratified sampling without labels; one immutable fixed look.

    Each row is retained or excluded with a recorded pre-outcome reason. The
    manifest contains stratum sizes, inclusion probabilities and analysis weights.
    """
    if not isinstance(inventory, dict) or set(inventory) != {'benchmark', 'revision', 'license', 'tasks'}:
        raise ValueError('Inventory requires only benchmark, revision, license and tasks')
    if any(not isinstance(inventory[k], str) or not inventory[k].strip()
           for k in ('benchmark', 'revision', 'license')):
        raise ValueError('Pin benchmark revision and license before sampling')
    if not _hex(inventory['revision'], 40) and not _hex(inventory['revision'], 64):
        raise ValueError('Revision must be an immutable 40/64-hex commit or content identity, not main/latest')
    if type(n) is not int or n < 1 or type(seed) is not int:
        raise ValueError('Require positive integer n and an explicit integer seed')
    if not isinstance(inventory['tasks'], list) or not inventory['tasks']:
        raise ValueError('A complete metadata inventory is required')
    groups: dict[str, list[dict]] = defaultdict(list)
    ids = set()
    exclusions = []
    allowed = {'id', 'source_group', 'stratum', 'content_sha256', 'exposure', 'eligible', 'exclusion_reason'}
    for task in inventory['tasks']:
        if not isinstance(task, dict) or set(task) != allowed:
            raise ValueError('Unexpected metadata fields; do not pass outcomes or cost-ranked task lists')
        if any(not isinstance(task[k], str) or not task[k].strip() for k in ('id', 'source_group', 'stratum')):
            raise ValueError('Nonempty task/source/stratum identifiers required')
        if task['id'] in ids or not _hex(task['content_sha256']):
            raise ValueError('Duplicate task or invalid content hash')
        ids.add(task['id'])
        if task['exposure'] not in {'unseen', 'development', 'previous_evaluation'} or type(task['eligible']) is not bool:
            raise ValueError('Explicit exposure/eligibility metadata required')
        if not isinstance(task['exclusion_reason'], str):
            raise ValueError('Exclusion reason must be text')
        if task['eligible'] and task['exposure'] == 'unseen':
            if task['exclusion_reason']:
                raise ValueError('An eligible unseen row cannot have an exclusion reason')
            groups[task['stratum']].append(task)
        else:
            if not task['exclusion_reason'].strip():
                raise ValueError('Every exclusion needs a recorded pre-outcome reason')
            exclusions.append(task)
    population = sum(len(v) for v in groups.values())
    if not len(groups) <= n <= population:
        raise ValueError('Sample must cover every stratum and fit the eligible inventory')
    # Constrained proportional allocation. Disclose inclusion probabilities;
    # do NOT silently treat minimum-one oversampling as simple random sampling.
    allocation = {k: 1 for k in groups}
    while sum(allocation.values()) < n:
        key = max((k for k in groups if allocation[k] < len(groups[k])),
                  key=lambda k: (n * len(groups[k]) / population - allocation[k], k))
        allocation[key] += 1
    rng = random.Random(seed)
    selected = []
    strata = []
    for key in sorted(groups):
        tasks = sorted(groups[key], key=lambda t: t['id'])
        rng.shuffle(tasks)
        count = allocation[key]
        selected.extend(tasks[:count])
        strata.append({'stratum': key, 'population_n': len(tasks), 'sample_n': count,
                       'inclusion_probability': count / len(tasks), 'population_weight': len(tasks) / population})
    rng.shuffle(selected)
    plan = {'schema': 1, 'kind': 'frozen_metadata_subset', 'inventory_sha256': digest(inventory),
            'benchmark': inventory['benchmark'], 'revision': inventory['revision'], 'license': inventory['license'],
            'population_n': population, 'sample_n': n, 'seed': seed, 'strata': strata,
            'selected': selected, 'exclusions': sorted(exclusions, key=lambda t: t['id']),
            'analysis': {'fixed_look': True, 'early_success_stopping': False,
                         'max_observed_additional_failures': 0, 'quality_tolerance': 0.0,
                         'selection_uses_results': False, 'sampling_inventory_completeness_attested_by': 'operator'},
            'official_full_benchmark_score': False, 'formal_gate_passed': False,
            'paid_evaluation_authorized': False}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as f:
        f.write(canonical(plan))
    return {'plan_sha256': digest(plan), 'sample_n': n, 'strata': strata,
            'trials_for_two_conditions_one_repeat': 2 * n, 'paid_calls': 0,
            'official_full_benchmark_score': False, 'paid_evaluation_authorized': False}


def compare_subset(plan: dict, expected_sha: str, results: list[dict]) -> dict:
    """Controller-supplied official labels/costs, not an LLM judge or certification.

    False is an observed official failure. Null is unknown/pending. Neither may
    be dropped. Confidence bounds are explicitly conditional on source/sampling
    assumptions; zero tolerance is never silently relaxed to five percent.
    """
    if digest(plan) != expected_sha or plan.get('kind') != 'frozen_metadata_subset':
        raise ValueError('Subset identity mismatch')
    selected = {t['id']: t for t in plan['selected']}
    rows = {}
    keys = {'id', 'baseline_success', 'candidate_success', 'baseline_tokens', 'candidate_tokens',
            'baseline_result_sha256', 'candidate_result_sha256'}
    if not isinstance(results, list):
        raise ValueError("Paired results must be an explicit list")
    for r in results:
        if not isinstance(r, dict) or set(r) != keys or not isinstance(r['id'], str) or r['id'] not in selected or r['id'] in rows:
            raise ValueError('Unexpected or duplicate paired outcome')
        for k in ('baseline_success', 'candidate_success'):
            if r[k] is not None and type(r[k]) is not bool:
                raise ValueError('Success must be an official boolean or null')
        for k in ('baseline_tokens', 'candidate_tokens'):
            if r[k] is not None and (type(r[k]) is not int or r[k] < 0):
                raise ValueError('Known token counts must be nonnegative integers')
        for k in ('baseline', 'candidate'):
            if r[k + '_success'] is not None and not _hex(r[k + '_result_sha256']):
                raise ValueError('An observed label needs an official result identity')
            if r[k + '_result_sha256'] is not None and not _hex(r[k + '_result_sha256']):
                raise ValueError('Invalid result hash')
        rows[r['id']] = r
    complete = len(rows) == len(selected) and all(r['baseline_success'] is not None and
                r['candidate_success'] is not None for r in rows.values())
    unique_groups = len({t['source_group'] for t in selected.values()}) == len(selected)
    harms = sum(r['baseline_success'] is True and r['candidate_success'] is False for r in rows.values())
    wins = sum(r['baseline_success'] is False and r['candidate_success'] is True for r in rows.values())
    stats = []
    delta = weighted_harm_upper = 0.0
    for s in plan['strata']:
        ids = [k for k, t in selected.items() if t['stratum'] == s['stratum']]
        if complete:
            h = sum(rows[k]['baseline_success'] and not rows[k]['candidate_success'] for k in ids)
            w = sum(not rows[k]['baseline_success'] and rows[k]['candidate_success'] for k in ids)
            bound = harm_upper_bound(h, len(ids), alpha=0.05 / len(plan['strata'])) if unique_groups else None
            delta += s['population_weight'] * (w - h) / len(ids)
            if bound is not None:
                weighted_harm_upper += s['population_weight'] * bound
            stats.append({**s, 'harms': h, 'wins': w, 'conditional_harm_upper': bound})
    known_costs = len(rows) == len(selected) and all(r['baseline_tokens'] is not None and
                         r['candidate_tokens'] is not None for r in rows.values())
    base = sum(r['baseline_tokens'] for r in rows.values()) if known_costs else None
    cand = sum(r['candidate_tokens'] for r in rows.values()) if known_costs else None
    return {'schema': 1, 'plan_sha256': expected_sha, 'allocated_pairs': len(selected),
            'reported_pairs': len(rows), 'all_labels_known': complete, 'independent_source_ids': unique_groups,
            'harms': harms, 'wins': wins, 'weighted_quality_difference': delta if complete else None,
            'conditional_weighted_harm_upper_95': weighted_harm_upper if complete and unique_groups else None,
            'strata': stats, 'known_baseline_tokens': base, 'known_candidate_tokens': cand,
            'sample_token_reduction': 1 - cand / base if base and cand is not None else None,
            'no_observed_additional_failure': complete and harms == 0,
            'quality_noninferiority_proven': False, 'strict_zero_loss_proven': False,
            'official_full_benchmark_score': False, 'formal_gate_passed': False,
            'assumptions': ['fixed candidate and fixed one-look sample', 'pre-outcome complete metadata inventory',
                            'independent sampled source clusters within each stratum',
                            'reported labels and total costs require separate provenance verification'],
            'interpretation': 'An observed no-harm result is not proof of zero population degradation. '
                              'Do not use this report for repeated success peeking or claim full-benchmark performance.'}
