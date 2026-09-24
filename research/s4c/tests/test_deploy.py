"""Deployed-mode wiring: the frozen gate must decide, refuse, and never guess a feature."""
from __future__ import annotations
import json, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research/s4"))
sys.path.insert(0, str(ROOT / "research/s4c"))

from reverpi_sources.selector import action, decide  # noqa: E402
from reverpi.errors import LabError  # noqa: E402

FIT = Path(__file__).resolve().parents[1] / "testdata" # historical arithmetic, not current permission
FEATURES = ("eligible_bytes", "history_bytes", "eligible_count", "prefix_requests", "last_cached_fraction")


def feature_vector(history_bytes, cached=0.9):
    return {"eligible_bytes": 20000, "history_bytes": history_bytes, "eligible_count": 1,
            "prefix_requests": 3, "last_cached_fraction": cached}


@pytest.fixture(scope="module")
def artifacts():
    candidate = json.loads((FIT / "RULE.json").read_text())
    passing = json.loads((FIT / "CALIB_MARGIN_30_FINAL.json").read_text())
    failing = json.loads((FIT / "CALIB_MARGIN_05_ALL.json").read_text())
    return candidate, passing, failing


def test_passing_gate_routes_on_the_fitted_threshold(artifacts):
    candidate, passing, _ = artifacts
    assert passing["deployable"] is True and passing["calibration_passed"] is True
    threshold = candidate["rule"]["threshold"]
    assert decide(candidate, passing, feature_vector(threshold + 1)) == "projected"
    assert decide(candidate, passing, feature_vector(threshold - 1)) == "full"


def test_failing_gate_falls_back_to_full_and_never_deploys(artifacts):
    candidate, _, failing = artifacts
    assert failing["deployable"] is False
    # A non-deployable artifact must never select projection, whatever the features say.
    assert decide(candidate, failing, feature_vector(10 ** 6)) == "full"


def test_missing_cache_feature_is_refused_not_defaulted(artifacts):
    candidate, passing, _ = artifacts
    broken = feature_vector(10 ** 6, cached=None)
    with pytest.raises(LabError):
        decide(candidate, passing, broken)
    with pytest.raises(LabError):
        action(candidate["rule"], broken)


def test_decision_is_bound_to_the_artifacts_it_used(artifacts):
    candidate, passing, _ = artifacts
    body = {k: v for k, v in candidate.items() if k != "candidate_sha256"}
    from reverpi.util import digest
    assert digest(body) == passing["candidate_sha256"] == candidate["candidate_sha256"]
    # The calibration artifact records a digest of the exact record set it judged;
    # the deployment decision carries both digests forward so a mismatch is detectable.
    assert passing["sources"] == 9 and passing["sources_with_observed_harm"] == 0
    assert passing["risk_margin"] == 0.30 and passing["upper_bound"] <= passing["risk_margin"]
