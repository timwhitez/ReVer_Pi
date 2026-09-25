"""Online-projection admission: research identity vs operator-declared profile (issue #14).

Only create_app/doctor are exercised; no HTTP request is sent to any Provider.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
import pytest
from reverpi.config import Provider, StudyConfig, online_admission
from reverpi.errors import LabError
from reverpi.gateway import create_app
from reverpi.util import canonical, digest

ROOT = Path(__file__).resolve().parents[1]
LOCAL = dict(name='local', model='local-reasoning-model', effort='low', concurrency=1, mock=False,
             base_url='https://api.example.invalid/v1')


def study(mode, admission=None):
    online = {'mode': mode, **({'admission': admission} if admission else {})}
    return StudyConfig(methods=['mask'], online_projection=online)


async def build(provider, config, run):
    app = create_app(provider, config, run)
    await app.state.client.close()
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
async def test_custom_label_research_profile(tmp_path, protocol):
    provider = Provider(**LOCAL, protocol=protocol)
    await build(provider, study('off'), tmp_path / 'off')
    for mode in ('observe', 'apply'):
        run = tmp_path / mode
        with pytest.raises(LabError) as err:
            create_app(provider, study(mode), run)
        assert err.value.kind == 'online_profile' and not run.exists()
        # The refusal names the actual constraint and the explicit way forward.
        assert 'not a frozen research identity' in err.value.message
        assert 'operator_declared' in err.value.message and 'expected_response_model' in err.value.message


@pytest.mark.parametrize('change', [{'model': 'other-model'}, {'effort': 'high'}, {'concurrency': 2}])
def test_research_profile_still_refuses_changed_identity(tmp_path, change):
    provider = Provider(**{**LOCAL, 'model': 'deepseek-flash', **change})
    run = tmp_path / 'run'
    with pytest.raises(LabError) as err:
        create_app(provider, study('apply'), run)
    assert err.value.kind == 'online_profile' and not run.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('protocol', ['chat_completions', 'responses'])
@pytest.mark.parametrize('mode', ['observe', 'apply'])
async def test_operator_declared_admits_uncertified_model_under_contract(tmp_path, protocol, mode):
    provider = Provider(**LOCAL, protocol=protocol, expected_response_model='local-reasoning-model')
    app = await build(provider, study(mode, 'operator_declared'), tmp_path / 'run')
    assert app.state.projection is not None
    report = online_admission(provider, study(mode, 'operator_declared'))
    assert report['allowed'] and report['certified'] is False and report['basis'] == 'operator_declared_uncertified'


@pytest.mark.parametrize('change,needle', [
    ({}, 'expected_response_model'),
    ({'expected_response_model': 'm', 'effort': 'high'}, 'provider.effort'),
    ({'expected_response_model': 'm', 'concurrency': 4}, 'provider.concurrency'),
])
def test_operator_declared_contract_is_enforced(tmp_path, change, needle):
    provider = Provider(**{**LOCAL, **change})
    run = tmp_path / 'run'
    with pytest.raises(LabError) as err:
        create_app(provider, study('apply', 'operator_declared'), run)
    assert err.value.kind == 'online_profile' and needle in err.value.message and not run.exists()


def test_default_admission_keeps_frozen_config_digest():
    """Configs written before the admission field existed hash exactly as before."""
    config = StudyConfig(methods=['mask'], online_projection={'mode': 'apply'})
    dump = config.model_dump()
    assert 'admission' not in dump['online_projection']
    assert StudyConfig.model_validate(dump) == config
    declared = study('apply', 'operator_declared').model_dump()
    assert declared['online_projection']['admission'] == 'operator_declared'
    assert digest(canonical(declared)) != digest(canonical(dump))


def test_research_identity_is_certified_and_mock_is_protocol_only():
    flash = Provider(**{**LOCAL, 'model': 'deepseek-flash'})
    assert online_admission(flash, study('apply'))['certified'] is True
    mock = Provider(**{**LOCAL, 'mock': True, 'effort': 'high', 'concurrency': 4})
    assert online_admission(mock, study('apply'))['basis'] == 'mock_provider_protocol_only'


def test_doctor_reports_admission_without_network(tmp_path):
    provider = tmp_path / 'provider.json'
    provider.write_text(json.dumps({**LOCAL, 'protocol': 'responses'}))
    config = tmp_path / 'study.json'
    config.write_text(json.dumps({'methods': ['mask'], 'online_projection': {'mode': 'apply'}}))
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    r = subprocess.run([sys.executable, '-B', '-m', 'reverpi', 'doctor', '--provider', str(provider), '--config', str(config)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    info = json.loads(r.stdout)
    assert info['network_called'] is False
    assert info['online_projection']['allowed'] is False
    assert any('not a frozen research identity' in p for p in info['online_projection']['problems'])
