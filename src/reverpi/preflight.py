"""Zero-dispatch checks. No provider probe, verifier execution, or ledger claim.

The budget screen is a declared-output stress test, not an estimate of expected
cost or proof of a physical billing cap. Gold validation belongs to the trusted
controller; only hashes and public question IDs enter a run's certificate.
"""
from __future__ import annotations
import os
from pathlib import Path
import ssl
from typing import Any

from .config import Provider, CompressionConfig
from .errors import LabError
from .protocols import build_request, conservative_input_tokens
from .util import bytes_digest, canonical, digest, strict_json_loads, source_manifest, atomic_create


def validate_provider_environment(provider: Provider) -> dict[str, Any]:
    """Validate local prerequisites without returning secret values or making I/O requests."""
    if provider.mock:
        return {'mock': True, 'paid_calls': 0, 'credentials_checked': False}
    try:
        secret = provider.secret()
        # httpx header values are ASCII; catch these local errors BEFORE an op claim.
        if not secret.strip() or any(ord(c) < 33 or ord(c) > 126 for c in secret):
            raise ValueError('bad credential bytes')
    except (ValueError, TypeError):
        raise LabError('configuration_preflight',
                       f'Missing or invalid credential environment variable: {provider.api_key_env}',
                       ambiguous=False) from None
    for name in provider.extra_headers_env.values():
        value = os.environ.get(name)
        if (not value or value != value.strip(" \t")
                or any(ord(c) < 32 or ord(c) > 126 for c in value)):
            raise LabError('configuration_preflight',
                           f'Missing or invalid header environment variable: {name}', ambiguous=False)
    if provider.ca_bundle_env:
        try:
            value = os.environ.get(provider.ca_bundle_env)
            if not value:
                raise ValueError('missing CA file')
            ssl.create_default_context(cafile=value)
        except (ValueError, OSError, ssl.SSLError):
            raise LabError('configuration_preflight', 'Configured CA bundle is unavailable or invalid',
                           ambiguous=False) from None
    return {'mock': False, 'paid_calls': 0, 'credentials_checked': True,
            'api_key_env': provider.api_key_env, 'remote_authentication_verified': False}


def validate_gold_mapping(raw: bytes, expected_sha: str, question_ids: list[str]) -> dict:
    """Pure controller-only validation shared with the final exact-string scorer."""
    if bytes_digest(raw) != expected_sha:
        raise ValueError('Gold differs from frozen commitment')
    obj = strict_json_loads(raw)
    if not isinstance(obj, dict) or set(obj) != {'answers'} or not isinstance(obj['answers'], dict):
        raise ValueError('Gold must contain only the answers mapping')
    answers = obj['answers']
    if len(question_ids) != len(set(question_ids)) or set(answers) != set(question_ids) or any(
        not isinstance(v, list) or not v or len(v) > 20 or
        any(not isinstance(s, str) for s in v) for v in answers.values()
    ):
        raise ValueError('Exact-match gold keys/variants do not match the frozen public questions')
    return answers


def certify_gold(parent: Path, parent_sha: str, gold: Path, gold_sha: str,
                 out: Path) -> dict:
    """Write a no-answer controller attestation. It is not a cryptographic trust authority."""
    from .paired_interventions import read_intervention
    obj = read_intervention(parent, parent_sha)
    ids = [q['id'] for q in obj['checkpoint']['questions']]
    validate_gold_mapping(gold.read_bytes(), gold_sha, ids)
    receipt = {'schema': 1, 'kind': 'controller_gold_schema_check', 'parent_sha256': parent_sha,
               'gold_sha256': gold_sha, 'question_ids': sorted(ids),
               'answer_values_included': False, 'gold_truth_certified': False,
               'trusted_local_controller_required': True}
    out.parent.mkdir(parents=True, exist_ok=True)
    # Never overwrite an old certificate, result, or supplied gold file.
    atomic_create(out, canonical(receipt))
    return {'certificate_sha256': digest(receipt), 'paid_calls': 0, **receipt}


def validate_gold_certificate(obj: dict, parent: dict, parent_sha: str, gold_sha: str) -> None:
    ids = sorted(q['id'] for q in parent['checkpoint']['questions'])
    expected = {'schema': 1, 'kind': 'controller_gold_schema_check', 'parent_sha256': parent_sha,
                'gold_sha256': gold_sha, 'question_ids': ids, 'answer_values_included': False,
                'gold_truth_certified': False, 'trusted_local_controller_required': True}
    if not isinstance(obj, dict) or canonical(obj) != canonical(expected):
        raise LabError('evaluation_preflight', 'Controller schema certificate is missing or mismatched')


def budget_screen(folder: Path, plan: dict) -> dict:
    """Exact first-request reservations and a no-retry, output-only stress envelope.

Reservations are not summed as realized spend: settled reservations are released.
The envelope intentionally asks whether a full-output stress scenario could fit.
Passing is only a necessary screen, NOT sufficient funding for later inputs,
retries, tool receipts, unknown usage, task success, or an unbounded upstream.
"""
    provider = Provider.model_validate(plan['provider'])
    rows = []
    if plan['kind'] == 'development_same_parent_matrix':
        from .paired_interventions import Arm, initial_reader_request, read_intervention
        from .revalidation import load_registry
        obj = read_intervention(folder/'parent.json', plan['parent_sha256'])
        specs = load_registry(folder/'registry.json', plan['registry_sha256'])
        config = CompressionConfig.model_validate(plan['archive'])
        turns = 0
        for unit in plan['units']:
            arm = Arm(**unit['arm'])
            messages, tools = initial_reader_request(obj, arm, config, list(specs))
            _, body = build_request(provider, messages, tools or None)
            inp = conservative_input_tokens(body)
            rows.append({'unit': unit['unit'], 'declared_max_turns': arm.max_turns, 'first_input_reservation': inp,
                         'first_total_reservation': inp + provider.max_output_tokens})
            turns += arm.max_turns
    elif plan['kind'] == 'flash_scoped_review':
        from .lean_review import review_messages
        for job in plan['jobs']:
            _, body = build_request(provider, review_messages(job))
            inp = conservative_input_tokens(body)
            rows.append({'unit': f"job-{job['index']}", 'declared_max_turns': 1, 'first_input_reservation': inp,
                         'first_total_reservation': inp + provider.max_output_tokens})
        turns = len(rows)
    else:
        raise ValueError('Unsupported budget-screen plan')
    if not rows:
        raise ValueError('Empty workload')
    budget = plan['budget']
    total = budget['max_total_tokens']
    cell = budget['per_cell_tokens']
    output = provider.max_output_tokens
    one_shot = sum(x['first_total_reservation'] for x in rows)
    declared_output_stress = turns * output  # no retries and input costs OMITTED, not a sufficient bound
    first_fit = all(x['first_total_reservation'] <= min(total, cell) for x in rows)
    cell_stress_fit = all(x['declared_max_turns'] * output <= cell for x in rows)
    attempts_fit = turns <= budget['max_attempts'] and all(
        x['declared_max_turns'] <= budget['per_cell_attempts'] for x in rows)
    screened = first_fit and one_shot <= total and declared_output_stress <= total and cell_stress_fit and attempts_fit
    projection_sha = digest(source_manifest(Path(__file__).resolve().parents[2]))
    return {'schema': 1, 'kind': 'non_sufficient_budget_pressure_screen', 'paid_calls': 0,
            'necessary_not_sufficient_screen': True,
            'allocated_tokens': total, 'per_cell_tokens': cell, 'output_reservation': output,
            'planned_units': len(rows), 'declared_max_model_turns_no_retry': turns,
            'first_requests': rows, 'each_initial_request_fits_empty_slot': first_fit,
            'one_call_per_unit_full_reservation_scenario': one_shot,
            'declared_output_only_stress_tokens': declared_output_stress,
            'output_only_stress_shortfall': max(0, declared_output_stress-total),
            'stress_screen_passed': screened, 'output_pressure_screen_passed': screened,
            'legacy_stress_screen_passed_alias': True, 'full_workload_completion_guaranteed': False, 'per_cell_output_stress_fits': cell_stress_fit,
            'declared_turn_attempt_caps_fit': attempts_fit, 'completion_funding_certified': False,
            'expected_spend_estimated': False, 'physical_billing_cap_proven': False,
            'projection_source_sha256': projection_sha,
            'projection_matches_frozen_source': plan.get('source_sha256') == projection_sha,
            'interpretation': 'A failed screen means this plan does not fund its declared stress scenario; '
                              'it does NOT prove that low-output trajectories cannot finish. '
                              'Passing does not fund unknown future inputs or retries.'}
