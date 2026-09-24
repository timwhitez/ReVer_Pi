from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import verify_paired_prefix as auditor


def test_mixed_recovery_rejection_is_audited_without_claiming_exact_recovery(monkeypatch):
    original = auditor.transcript
    text = ['ReVer halted: recovery_arguments']

    def transcript(folder):
        results, calls = original(folder)
        if folder.name == 'projected':
            calls['mixed-recovery'] = {'name': 'recover_evidence',
                                       'arguments': {'handle': 'a' * 64, 'query': 'marker'}}
            results['mixed-recovery'] = {'text': text[0], 'is_error': False,
                                         'tool_name': 'recover_evidence'}
        return results, calls

    monkeypatch.setattr(auditor, 'transcript', transcript)
    run = ROOT / 'reports/rc7/sealed_profile/responses_long'
    result = auditor.audit(run)
    projected = next(p for p in result['phases'] if p['phase'] == 'projected')
    assert result['first_pair_identical_except_projection'] is True
    assert projected['exact_recovery_calls'] == 1
    assert projected['rejected_recovery_calls'] == 1

    text[0] = '{"text":"forged recovery"}'
    with pytest.raises(ValueError, match='Mixed recovery arguments were not rejected'):
        auditor.audit(run)
