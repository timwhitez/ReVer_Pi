"""Reproductions and cross-file adjudications from the gen9 review evidence."""
import json
import os
import sys
from pathlib import Path
import pytest
from reverpi.acceptance import post_compaction_assistant
from reverpi.bundle_audit import audit_review
from reverpi.data import freeze
from reverpi.devtasks import generate_tasks, validate_oracles
from reverpi.errors import LabError
from reverpi.revalidation import VerifierSpec, execute_verifier

@pytest.mark.parametrize('events',[[],[{'type':'agent_end'}],[{'type':'message_end','message':{'role':'user'}}],
    [{'type':'message_end','message':{'role':'assistant','stopReason':'error'}}]])
def test_old_history_or_empty_prompt_events_cannot_certify_continuation(events):
    with pytest.raises(LabError,match='new usable'):post_compaction_assistant(events)

def test_new_assistant_event_is_accepted():
    m={'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'current'}]}
    assert post_compaction_assistant([{'type':'message_end','message':m}])==m

def test_public_gold_alias_rejected_before_parsing(tmp_path):
    p=tmp_path/'input';p.write_text('not even a dataset')
    with pytest.raises(LabError,match='distinct'):
        freeze(tmp_path,tmp_path/'config',p,p,[p],tmp_path/'frozen.json')

def test_review_control_symlink_refused(tmp_path):
    data=tmp_path/'r';data.mkdir();outside=tmp_path/'outside';outside.write_text('{}')
    (data/'review_manifest.json').symlink_to(outside)
    with pytest.raises(ValueError,match='Symlink'):audit_review(tmp_path,data)

def test_owned_oracle_validator_rejects_mutation_without_executing_it(tmp_path):
    out=tmp_path/'tasks';generate_tasks(out)
    sentinel=tmp_path/'MUST_NOT_EXIST'
    (out/'utf8-prefix/tests/check.py').write_text(f'open({str(sentinel)!r},"w").write("bad")')
    with pytest.raises(ValueError,match='differs'):validate_oracles(out)
    assert not sentinel.exists()

def test_owned_oracles_still_match_all_generated_tasks(tmp_path):
    out=tmp_path/'tasks';generate_tasks(out)
    assert validate_oracles(out)['all_valid']

def test_verifier_path_never_includes_current_or_relative_directory(tmp_path,monkeypatch):
    (tmp_path/'check.py').write_text('import os; print(os.environ["PATH"])')
    monkeypatch.setattr(os,'defpath',':/usr/bin:relative:/bin:.')
    r=execute_verifier(VerifierSpec('check',(sys.executable,'check.py'),('check.py',)),tmp_path)
    assert r['passed']
    assert '/usr/bin:/bin' in r['output']
