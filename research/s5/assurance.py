"""Deployment permission is not a boolean copied from an exploratory fit.

Prospective assessments bind a candidate, a full task frame, scoring/model
identities, an analysis plan, and a separately committed risk authorization.
They remain conditional on operator attestations of sampling/chronology. The
implementation cannot infer those facts from hashes. Currency and paid-request
authorization are deliberately separate and are never granted by this module.
"""
from __future__ import annotations
import math
from .contracts import digest, require
from .statistics import binomial_upper, spending_upper, group_identification

METRIC = "source_any_selected_harm_at_frozen_budget"
SCHEMA = "reverpi.s5.gate.v1"

def _number(x):
    return type(x) in (int,float) and math.isfinite(x)

def candidate_identity(candidate: dict) -> str:
    require(type(candidate) is dict, "candidate_required")
    body = dict(candidate); sha = body.pop("candidate_sha256", None)
    require(sha == digest(body), "candidate_identity_mismatch")
    return sha

def evaluate_rule(candidate: dict, features: dict) -> str:
    """Evaluate only the frozen rule's required pre-action feature."""
    candidate_identity(candidate)
    require(type(features)is dict,'features_required')
    rule=candidate['rule']
    if rule.get('kind')=='constant':
        require(set(rule)=={'kind','project'} and type(rule['project'])is bool,'invalid_constant_rule')
        return 'projected' if rule['project'] else 'full'
    require(set(rule)=={'kind','feature','threshold','direction'} and rule['kind']=='threshold' and rule['direction']in {'ge','le'},'invalid_threshold_rule')
    allowed={'eligible_bytes','history_bytes','eligible_count','prefix_requests','last_cached_fraction'}
    require(rule['feature'] in allowed and _number(rule['threshold']) and rule['threshold']>=0,'invalid_rule_feature')
    value=features.get(rule['feature'])
    require(_number(value) and value>=0 and (rule['feature']!='last_cached_fraction' or value<=1),'missing_or_invalid_required_feature')
    chosen=value>=rule['threshold'] if rule['direction']=='ge' else value<=rule['threshold']
    return 'projected' if chosen else 'full'

def inspect_legacy_gate(candidate: dict, calibration: dict, authorization: dict) -> dict:
    """Audit a submitted S4 artifact without rewriting or legitimizing it."""
    reasons=[]
    try:
        sha=candidate_identity(candidate)
        if calibration.get("candidate_sha256") != sha: reasons.append("candidate_mismatch")
    except ValueError:
        sha=None;reasons.append("candidate_identity_invalid")
    cb=dict(calibration);cs=cb.pop("calibration_sha256",None)
    if cs != digest(cb):reasons.append("calibration_identity_invalid")
    if authorization.get("approved") is not True:reasons.append("risk_authorization_missing")
    margin=authorization.get("risk_margin")
    requested=calibration.get("risk_margin")
    if not _number(margin) or not 0 <= margin < 1: reasons.append("authorized_margin_invalid")
    elif not _number(requested) or requested>margin: reasons.append("risk_margin_exceeds_authorization")
    if authorization.get("alpha") != calibration.get("alpha"):reasons.append("alpha_not_authorized")
    scope=authorization.get("scope",{})
    if scope.get("candidate_sha256") != sha:reasons.append("authorization_candidate_mismatch")
    if scope.get("calibration_data_sha256") != calibration.get("data_sha256"):
        reasons.append("authorization_dataset_mismatch")
    reasons.extend(["no_independently_committed_sampling_frame",
                    "no_prospective_stopping_contract",
                    "complete_pair_selection_not_resolved",
                    "scorer_and_runtime_not_bound_to_gate"])
    return {"schema":"reverpi.s5.legacy-audit.v1", "historical_deployable_flag":calibration.get("deployable"),
            "candidate_sha256":sha,"authorization_margin":margin,"evaluated_margin":requested,
            "reported_upper_bound":calibration.get("upper_bound"),
            "allow_projection":False,"reasons":sorted(set(reasons)),
            "historical_artifacts_modified":False,"new_paid_authorization":False}

def assess(candidate: dict, design: dict, authorization: dict, observations: list[dict], *,
           expected_design_sha256: str, expected_authorization_sha256: str) -> dict:
    """Conditional prospective assessment; default behavior is fail closed.

    The external digests must be supplied by a trusted operator, not read back
    from the same mutable run directory. Passing this check is not proof of
    independent sampling; the exact assumptions are returned with the receipt.
    """
    sha=candidate_identity(candidate)
    require(digest(design)==expected_design_sha256, "independent_design_digest_mismatch")
    require(digest(authorization)==expected_authorization_sha256, "independent_authorization_digest_mismatch")
    expected={"schema","candidate_sha256","model_fork","scorer_sha256","runtime_sha256", "metric",
              "frame","method","alpha","risk_margin","precommitted_before_outcomes", "independent_sampling_attested",
              "selection_not_outcome_dependent","candidate_frozen_before_calibration","development_source_groups"}
    require(type(design) is dict and set(design)==expected and design["schema"]=="reverpi.s5.design.v1", "invalid_design_schema")
    require(design["candidate_sha256"]==sha and design["model_fork"]==candidate.get("model_fork"),"design_candidate_or_model_mismatch")
    require(design["metric"]==METRIC,"unsupported_risk_metric")
    require(design["method"] in {"fixed_sample","alpha_spending"},"unsupported_inference_method")
    for key in ("scorer_sha256","runtime_sha256"):
        value=design[key];require(type(value)is str and len(value)==64 and all(c in '0123456789abcdef' for c in value),"invalid_identity")
    for key in ("precommitted_before_outcomes","independent_sampling_attested","selection_not_outcome_dependent","candidate_frozen_before_calibration"):
        require(type(design[key]) is bool,"invalid_attestation_type")
    margin=design["risk_margin"];alpha=design["alpha"]
    require(_number(margin) and 0<=margin<1 and _number(alpha) and 0<alpha<1,"invalid_risk_parameters")
    require(type(design["development_source_groups"])is list and all(type(s)is str for s in design["development_source_groups"]),"invalid_development_groups")
    require(set(design["development_source_groups"]) == set(candidate.get("development_groups",[])),"development_frame_mismatch")
    require(type(design['frame'])is dict,'invalid_sampling_frame')
    require(not(set(design["frame"])&set(design["development_source_groups"])),"source_leakage")
    auth_fields={"schema","approved","design_sha256","candidate_sha256","risk_margin","alpha","metric","model_fork"}
    require(type(authorization)is dict and set(authorization)==auth_fields and authorization["schema"]=="reverpi.s5.risk-authorization.v1", "invalid_authorization_schema")
    require(type(authorization["approved"])is bool,"invalid_authorization_type")
    require(_number(authorization['risk_margin']) and 0<=authorization['risk_margin']<1,"invalid_authorized_margin")
    reasons=[]
    if authorization['approved'] is not True:reasons.append('not_authorized')
    if authorization['design_sha256']!=expected_design_sha256:reasons.append('authorized_design_mismatch')
    if authorization['candidate_sha256']!=sha:reasons.append('authorized_candidate_mismatch')
    if authorization['risk_margin']<margin:reasons.append('risk_margin_exceeds_authorization')
    for key in ('alpha','metric','model_fork'):
        if authorization[key]!=design[key]:reasons.append('authorized_'+key+'_mismatch')
    for key in ('precommitted_before_outcomes','independent_sampling_attested','selection_not_outcome_dependent','candidate_frozen_before_calibration'):
        if not design[key]:reasons.append(key+'_missing')
    if candidate.get('synthetic') is not False:reasons.append('synthetic_or_unknown_candidate')
    require(type(observations)is list,'observations_required')
    reduced=[]
    fields={'task_id','source_group','eligible','features','full','projected'}
    for row in observations:
        require(type(row)is dict and set(row)==fields,'observation_schema_mismatch')
        require(type(row['eligible'])is bool and type(row['features'])is dict,'invalid_eligibility_or_features')
        choice=evaluate_rule(candidate,row['features']) if row['eligible'] else 'full'
        reduced.append({k:row[k] for k in ('task_id','source_group','full','projected')}|{'action':choice})
    identified=group_identification(design['frame'],reduced)
    if identified['observed_tasks'] != identified['planned_tasks']:
        reasons.append('incomplete_sampling_frame')
    if identified['unknown_groups']:
        reasons.append('unresolved_group_outcomes')
    n=identified['n'];k=identified['harm_upper_count']
    if design['method']=='fixed_sample':upper=binomial_upper(k,n,alpha);local=alpha
    else:
        value=spending_upper(k,n,alpha);upper=value['upper'];local=value['local_alpha']
    if upper>margin:reasons.append('risk_not_certified')
    body={'schema':SCHEMA,'candidate_sha256':sha,'design_sha256':expected_design_sha256,
          'authorization_sha256':expected_authorization_sha256,'observations_sha256':digest(observations),
          'model_fork':design['model_fork'],'scorer_sha256':design['scorer_sha256'],'runtime_sha256':design['runtime_sha256'],
          'method':design['method'],'metric':METRIC,'alpha':alpha,'local_alpha':local,'risk_margin':margin,
          'upper_bound':upper,'group_identification':identified,'allow_projection':not reasons,
          'reasons':sorted(set(reasons)),'scope':'conditional on declared independent source sampling and precommitment; not a task-level noninferiority claim',
          'paid_requests_authorized':False,'currency_savings_established':False}
    return {**body,'gate_sha256':digest(body)}

def validate_runtime_binding(candidate: dict, gate: dict | None, *,
                             expected_gate_sha256: str | None, model_fork: str,
                             runtime_sha256: str, scorer_sha256: str) -> bool:
    """Validate permission before any paid capture; absence is conservative full."""
    sha=candidate_identity(candidate)
    if gate is None:
        require(expected_gate_sha256 is None,'gate_file_missing')
        return False
    require(expected_gate_sha256 is not None,'external_gate_digest_required')
    body=dict(gate);h=body.pop('gate_sha256',None)
    require(h==digest(body)==expected_gate_sha256,'gate_identity_mismatch')
    require(gate.get('schema')==SCHEMA and gate.get('candidate_sha256')==sha,'gate_candidate_mismatch')
    require(gate.get('model_fork')==model_fork==candidate.get('model_fork'),'runtime_model_mismatch')
    require(gate.get('runtime_sha256')==runtime_sha256 and gate.get('scorer_sha256')==scorer_sha256,'runtime_or_scorer_mismatch')
    require(type(gate.get('allow_projection'))is bool and type(gate.get('reasons'))is list,'invalid_gate_result')
    require(not gate['allow_projection'] or not gate['reasons'],'contradictory_gate_result')
    return gate['allow_projection']


def checked_action(candidate: dict, features: dict, gate: dict | None, *,
                   expected_gate_sha256: str | None=None, model_fork: str,
                   runtime_sha256: str, scorer_sha256: str) -> str:
    """Runtime defaults to full; a provided but unbound/invalid gate is an error."""
    permitted=validate_runtime_binding(candidate,gate,expected_gate_sha256=expected_gate_sha256,
                                      model_fork=model_fork,runtime_sha256=runtime_sha256,
                                      scorer_sha256=scorer_sha256)
    return evaluate_rule(candidate,features) if permitted else 'full'
