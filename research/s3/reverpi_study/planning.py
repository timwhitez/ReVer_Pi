"""Source-separated preregistration scaffolding, never an experiment launcher.

No provider imports, credentials, runner invocation or authorization. Content
hashes bind a supplied snapshot; provenance declarations still need human review.
"""
from __future__ import annotations
import hashlib
from .safeio import require,digest
from .frontier import integer

HEX=set('0123456789abcdef')


def h64(x):return type(x) is str and len(x)==64 and set(x)<=HEX

def validate_registry(rows):
    require(type(rows) is list and bool(rows),'Nonempty source registry required')
    ids=set();groups={};snapshots={}
    allowed={'instance_id','source_group','revision','content_sha256','provenance','split','stratum','replay_scope','inspected'}
    for r in rows:
        require(type(r) is dict and set(r)==allowed,'Registry fields must match schema exactly')
        for k in ('instance_id','source_group','revision','provenance','stratum'):
            require(type(r[k]) is str and 0<len(r[k])<=300 and '\x00' not in r[k],'Invalid text field: '+k)
        require(r['instance_id'] not in ids,'Duplicate instance ID');ids.add(r['instance_id'])
        require(h64(r['content_sha256']),'Invalid snapshot digest')
        require(r['split'] in {'development','calibration','external'},'Unknown split')
        require(type(r['inspected']) is bool,'inspected must be boolean')
        require(not(r['split'] in {'calibration','external'} and r['inspected']),'Inspected source cannot be held out')
        require(r['replay_scope']=='readonly_snapshot','Writable replay is not implemented')
        g=r['source_group'];split=r['split']
        require(g not in groups or groups[g]==split,'Source group crosses splits');groups[g]=split
        prev=snapshots.get(r['content_sha256'])
        require(prev is None or prev==(g,split),'Same snapshot relabelled across sources/splits')
        snapshots[r['content_sha256']]=(g,split)
    return {'instances':len(rows),'source_groups':len(groups),'splits':{s:sum(v==s for v in groups.values()) for s in ('development','calibration','external')},'independence_proven':False}


def plan(rows,*,seed,source_sha256,interface='split_v1',fork='luna',prefix_cap=6,suffix_cap=8):
    scope=validate_registry(rows)
    require(type(seed) is str and len(seed)>=8,'Explicit seed required')
    require(h64(source_sha256),'Source digest required')
    require(interface=='split_v1','This study freezes split_v1, no new interface cohorts')
    require(fork in {'luna','flash'},'Choose one fork, no pooled model results')
    integer(prefix_cap,'prefix_cap',1);integer(suffix_cap,'suffix_cap',1)
    require(prefix_cap<=30 and suffix_cap<=30,'Caps outside reviewed range')
    # Deterministic outcome-blind balanced order within source/stratum.
    bins={}
    for r in rows:bins.setdefault((r['source_group'],r['stratum']),[]).append(r)
    units=[]
    for key,items in sorted(bins.items()):
        def rank(r):return hashlib.sha256((seed+'\0'+r['instance_id']).encode()).hexdigest()
        items=sorted(items,key=rank)
        offset=int(hashlib.sha256((seed+'\0'+repr(key)).encode()).hexdigest(),16)%2
        for i,r in enumerate(items):
            order=['full','projected'] if (i+offset)%2==0 else ['projected','full']
            units.append({**r,'branch_order':order,'order_commitment':rank(r),
                          'retain_no_eligible':True,'resource_endpoints':list(range(1,suffix_cap+1))})
    units.sort(key=lambda r:r['order_commitment'])
    body={'schema':'reverpi.s3.design.v1','source_sha256':source_sha256,'registry_summary':scope,
        'seed':seed,'interface':interface,'fork':fork,'model_label':'gpt-6-luna' if fork=='luna' else 'deepseek-flash',
        'protocol':'responses','effort':'low','concurrency':1,'max_prefix_requests':prefix_cap,'max_suffix_requests':suffix_cap,
        'units':units,'paid_authorized':False,'authorized_request_count':0,'authorized_token_budget':0,
        'output_reservation':65536,'reservation_is_supplier_limit':False,'runtime_plan':False,
        'outcome_labels_permitted_in_registry':False,'population_noninferiority_margin':None,
        'fee_status':'unknown','dispatch_implementation':'NOT_IMPLEMENTED_FOR_ARBITRARY_SOURCES',
        'required_before_dispatch':['new explicit authorization','verified model/protocol route','matching runtime adapter for each readonly source','fixed source provenance and scorer','independent gold','budget admission and stop rules'],
        'endpoint_note':'S(k) from realized prefixes; smaller k is not a new policy run; all strata exploratory until fixed prospective design'}
    return {**body,'design_sha256':digest(body)}


def verify_plan(value):
    require(type(value) is dict,'Plan object required')
    copy=dict(value);h=copy.pop('design_sha256',None);require(h64(h) and digest(copy)==h,'Design digest mismatch')
    require(value.get('paid_authorized') is False and value.get('authorized_request_count')==0 and value.get('authorized_token_budget')==0,'Offline design cannot authorize spending')
    fields={'instance_id','source_group','revision','content_sha256','provenance','split','stratum','replay_scope','inspected'}
    rows=[{k:u[k] for k in fields} for u in value['units']]
    expected=plan(rows,seed=value['seed'],source_sha256=value['source_sha256'],interface=value['interface'],fork=value['fork'],prefix_cap=value['max_prefix_requests'],suffix_cap=value['max_suffix_requests'])
    require(value==expected,'Plan not reproducible from declared metadata')
    return True
