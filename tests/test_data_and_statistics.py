import json
from pathlib import Path
import pytest
from reverpi.data import make_fixtures,checkpoints,load_jsonl,audit,freeze,check_freeze,source_identity_keys
from reverpi.statistics import analyze,holm,mcnemar_exact
from reverpi.util import canonical
from reverpi.errors import LabError


def write(path,rows):path.write_text('\n'.join(canonical(x) for x in rows)+'\n')


def test_fixture_provenance_is_not_external(tmp_path):
    make_fixtures(tmp_path,3)
    rows=checkpoints(tmp_path/'public.jsonl')
    assert len(rows)==3 and all(x.split=='search' and x.synthetic for x in rows)
    assert len({x.source_group for x in rows})==1
    r=audit([tmp_path/'public.jsonl'])
    assert not r['conflicts'] and r['pretraining_contamination']=='unknown'


def test_cross_split_source_overlap(tmp_path):
    make_fixtures(tmp_path/'a',2);a=load_jsonl(tmp_path/'a/public.jsonl')
    b=[{**a[0],'id':'heldout','split':'external','synthetic':False}];write(tmp_path/'b.jsonl',b)
    r=audit([tmp_path/'a/public.jsonl',tmp_path/'b.jsonl'])
    assert any(x['kind']=='shared_source' for x in r['conflicts'])
    assert any(x['kind']=='identical_content' for x in r['conflicts'])


def test_provenance_metadata_not_identity():
    a=source_identity_keys('one',{'repo':'https://EXAMPLE/a.git/','license':'MIT','version':'1','issue':'7'})
    b=source_identity_keys('two',{'repository':'https://example/b','license':'MIT','version':'1','issue':'7'})
    assert not a&b
    c=source_identity_keys('three',{'repository_family':'https://example/a'})
    assert a&c=={'repo:https://example/a'}


def test_issue_requires_repository():
    with pytest.raises(ValueError,match='normalized repository'):source_identity_keys('x',{'issue':'7'})


def test_duplicate_ids_and_questions_rejected(tmp_path):
    make_fixtures(tmp_path,1);p=tmp_path/'public.jsonl';rows=load_jsonl(p)
    rows[0]['questions'].append(rows[0]['questions'][0]);write(p,rows)
    with pytest.raises(ValueError,match='unique'):checkpoints(p)


def test_freeze_change_detection_and_target_audit(tmp_path):
    root=tmp_path/'repo';(root/'src').mkdir(parents=True);(root/'src/a.py').write_text('x=1\n')
    make_fixtures(tmp_path/'data',2)
    cfg=tmp_path/'c.yaml';cfg.write_text('split: search\n')
    pub=tmp_path/'data/public.jsonl';gold=tmp_path/'data/gold.jsonl';out=tmp_path/'freeze.json'
    with pytest.raises(LabError,match='target dataset'):freeze(root,cfg,pub,gold,[],out)
    freeze(root,cfg,pub,gold,[pub],out);check_freeze(out,root,cfg,pub,gold)
    (root/'src/a.py').write_text('x=2\n')
    with pytest.raises(LabError,match='differ'):check_freeze(out,root,cfg,pub,gold)


def test_fake_external_fixtures_not_certified(tmp_path):
    make_fixtures(tmp_path/'data',1);pub=tmp_path/'data/public.jsonl';rows=load_jsonl(pub);rows[0]['split']='external';write(pub,rows)
    cfg=tmp_path/'c';cfg.write_text('x')
    with pytest.raises(LabError,match='external benchmarks'):
        freeze(tmp_path,cfg,pub,tmp_path/'data/gold.jsonl',[pub],tmp_path/'f')


def test_fixed_holm_family_and_monotonicity():
    assert holm([.01,None,.02])==pytest.approx([.03,1,.04])
    assert holm([.04,.01,.03])==pytest.approx([.06,.03,.06])
    with pytest.raises(ValueError):holm([float('nan')])
    assert mcnemar_exact(0,0)==1 and mcnemar_exact(10,0)==pytest.approx(2/1024)


def row(task,method,value,group=None,repeat=0):
    return {'task_id':task,'method':method,'success':value,'source_group':group or task,'repeat':repeat,'cost':{'known_tokens':10,'accounted_tokens':10}}


def test_missing_identification_not_confidence():
    result=analyze([row('a','full',True),row('a','new',None),row('b','full',False),row('b','new',True)],['full','new'],n_boot=20)
    pair=result['comparisons'][0]
    assert pair['risk_difference_identification_interval']==[0,.5]
    assert pair['cluster_bootstrap_95'] is None and pair['fixed_family_holm_p']==1


def test_repetitions_not_independent_tasks():
    rows=[row('a','full',True),row('a','new',False),row('a','new',True,repeat=1),row('a','new',True,repeat=2)]
    r=analyze(rows,['full','new'])
    assert r['table']['new']['n_planned']==1 and r['table']['new']['successes']==0


def test_clusters_no_pseudoreplication():
    rows=[row(str(i),m,m=='new',group='one-template') for i in range(10) for m in ['full','new']]
    r=analyze(rows,['full','new'])['comparisons'][0]
    assert r['source_clusters']==1 and r['independent_mcnemar_p'] is None and r['cluster_bootstrap_95'] is None


def test_compare_only_same_reference_panel():
    rows=[row('a','full',True),row('b','full',False),row('b','new',True),row('c','new',True)]
    r=analyze(rows,['full','new'])['comparisons'][0]
    assert r['paired_tasks']==1 and r['risk_difference_identification_interval']==[1,1]


def test_duplicate_or_inconsistent_rows_rejected():
    r=row('a','full',True)
    with pytest.raises(ValueError,match='Duplicate'):analyze([r,r],['full'])
    with pytest.raises(ValueError,match='boolean'):analyze([{**r,'success':1}],['full'])
    with pytest.raises(ValueError,match='source cluster'):analyze([r,row('a','new',True,group='other')],['full','new'])
