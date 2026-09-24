#!/usr/bin/env python3
"""Independently recheck the owned compaction fixture from wire/log/DB bytes.

Checks local integrity and interface semantics, not reviewer independence,
operator authenticity, model quality, natural triggers or supplier billing.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys
import hashlib
import shutil
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from reverpi.util import canonical, strict_json_loads, contained_regular_file, digest, atomic_write
from reverpi.research_audit import readonly_db
from reverpi.util import unseal_cache
from native_compaction_smoke import CONDITIONS, PROTOCOLS, PRESSURES, MARKER, wire_view, text, require


def expected_cells(protocols:list[str])->set[tuple[str,str,str]]:
    require(isinstance(protocols,list) and bool(protocols) and len(set(protocols))==len(protocols)
        and set(protocols)<=set(PROTOCOLS),'Invalid protocol coverage')
    return {(p,c,s) for p in protocols for s in PRESSURES
        for c in (('mask_checkpoint',) if s=='oversized_recent' else CONDITIONS)}


def validate_header(report:dict)->set[tuple[str,str,str]]:
    require(isinstance(report,dict) and report.get('schema')==1 and
        report.get('kind')=='explicit_boundary_compaction_contract','Wrong contract report')
    require(report.get('status')=='passed','Producer did not complete')
    require(type(report.get('paid_model_calls')) is int and report['paid_model_calls']==0
        and report.get('scripted_actor') is True and report.get('native_gate_eligible') is False
        and report.get('benchmark_or_quality_claim') is False
        and report.get('manual_checkpoint_not_default_agent_policy') is True,'Overstated authority/quality')
    expected=expected_cells(report.get('protocols'))
    rows=report.get('rows');require(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),'Malformed rows')
    actual=[(r.get('protocol'),r.get('condition'),r.get('pressure')) for r in rows]
    require(len(actual)==len(set(actual)) and set(actual)==expected,'Missing/duplicate fixture cells')
    return expected


def audit(root:Path)->dict:
    root=root.resolve(strict=True)
    def read(base:Path,name:str):return strict_json_loads(contained_regular_file(base,name).read_bytes())
    report=read(root,'report.json');expected=validate_header(report);verified=[]
    for item in report['rows']:
        key=(item['protocol'],item['condition'],item['pressure']);p,c,s=key
        name=f'{p}__{c}__{s}'
        require(item.get('case_dir')==name,'Case path mismatch')
        case=root/name
        row=read(case,'report.json');require(row==item,'Case/root report disagreement')
        st=read(case,'agent-config/settings.json');boundary=read(case,'boundary.json')
        require(st.get('compaction')=={'enabled':True,'keepRecentTokens':20000,'reserveTokens':66560},'Compaction settings changed')
        require(st==row.get('settings')==boundary.get('settings'),'Settings snapshots disagree')
        require(row.get('status')=='passed' and row.get('automatic_policy_verified') is False
            and row.get('native_gate_eligible') is False and row.get('session_revoked') is True
            and row.get('gateway_stopped') is True,'Invalid case status/authority')
        require(boundary.get('legal_preparation') is (s!='short') and boundary.get('usage_gate_crossed') is False,'Wrong preparation controls')
        require(boundary.get('pi_compaction_js_sha256')=='fcb12f1eb4d38578978e1a8e3e382a3fccfd5e0ccf87bc86979a9a8d9c145c7b','Dependency identity changed')
        requests=read(case,'requests.json');require(requests==row.get('requests') and isinstance(requests,list),'Wire index mismatch')
        follows=[]
        for i,r in enumerate(requests):
            require(r.get('index')==i and r.get('body_path')==f'wire_{i:03}.json','Unexpected request index/path')
            body=read(case,r['body_path']);messages,names=wire_view(body,p)
            require(digest(body)==r.get('body_sha256'),'Wire body digest mismatch')
            require(body.get('model')=='mock-reasoner','Unexpected model')
            require((body.get('reasoning_effort') if p=='chat_completions' else body.get('reasoning',{}).get('effort'))=='low','Effort differs')
            require((body.get('max_completion_tokens') if p=='chat_completions' else body.get('max_output_tokens'))==65536,'Output reservation differs')
            require(r.get('wire_content_bytes')==len(canonical(messages).encode()) and r.get('marker_in_wire') is (MARKER in canonical(messages)),'Wire measurements disagree')
            require(r.get('tool_names')==names,'Tool surface disagrees')
            if r.get('phase')=='followup':
                users=[m for m in messages if m.get('role')=='user']
                require(bool(users) and text(users[-1].get('content',[]))=='FOLLOWUP: confirm continuation using the current context.','Follow-up phase mislabelled')
                follows.append((r,messages))
        checkpoint=contained_regular_file(case,'checkpoint_session.jsonl').read_bytes()
        require(hashlib.sha256(checkpoint).hexdigest()==row.get('checkpoint_session_sha256'),'Checkpoint session hash mismatch')
        log=contained_regular_file(case,'session.jsonl').read_text()
        entries=[strict_json_loads(x) for x in log.splitlines() if x.strip()]
        commits=[x for x in entries if x.get('type')=='compaction']
        rpc=[strict_json_loads(x) for x in contained_regular_file(case,'rpc.jsonl').read_text().splitlines() if x.strip()]
        database=contained_regular_file(case,'gateway/sessions.sqlite')
        # These processes have stopped, and SQLite closed/checkpointed the files.
        # Refuse nonempty sidecars rather than silently ignoring them.
        for suffix in ('-wal','-journal'):
            side=Path(str(database)+suffix)
            require(not side.exists() or (not side.is_symlink() and side.is_file() and side.stat().st_size==0),'Pending SQLite sidecar')
        with readonly_db(database,immutable_snapshot=True) as db:
            state=db.execute('SELECT * FROM sessions').fetchall();ops=db.execute('SELECT * FROM compact_ops').fetchall()
        require(len(state)==1 and state[0]['disabled']==1,'Session lifecycle not closed')
        activated=c=='mask_checkpoint' and s=='long'
        rejected=s=='oversized_recent'
        require(state[0]['revision']==int(activated) and len(commits)==int(activated),'Commit evidence disagrees')
        if rejected:
            require(not follows and row.get('followup_dispatched') is False and row.get('original_history_unchanged') is True,'Continued after rejection')
            require(contained_regular_file(case,'session.jsonl').read_bytes()==checkpoint,'Rejected compaction changed checkpoint history')
            require(len(ops)==1 and ops[0]['state']=='failed' and state[0]['memory'] is None,'Capacity rejection did not fail closed')
            require(any(e.get('type')=='response' and e.get('command')=='compact' and e.get('success') is False for e in rpc),'No RPC rejection')
            require(any('compression_capacity' in str(e.get('message','')) for e in rpc),'Capacity failure not observed')
        else:
            require(len(follows)==1 and row.get('followup_dispatched') is True,'Missing follow-up')
            req,msg=follows[0];require(req==row.get('followup'),'Follow-up identity differs')
            require(req['marker_in_wire'] is (not activated),'Wrong visibility control')
            if activated:
                memory=unseal_cache(state[0]['memory'])
                require(len(ops)==1 and ops[0]['state']=='complete','No durable compaction commit')
                require(any(memory['text'] in text(m.get('content',[])) for m in msg if m.get('role')=='user'),'Committed memory not actually transmitted')
                require(commits[0].get('summary')==memory['text'],'Pi and gateway memories differ')
                from reverpi.canary_recovery import RecoveryProbe,verify_recovery
                archive=contained_regular_file(case,'gateway/archive.sqlite')
                for suffix in ('-wal','-journal'):
                    side=Path(str(archive)+suffix)
                    require(not side.exists() or (not side.is_symlink() and side.is_file() and side.stat().st_size==0),'Archive needs a complete snapshot')
                with tempfile.TemporaryDirectory(prefix='rever-archive-audit-') as tmp:
                    snapshot=Path(tmp)/'archive.sqlite';shutil.copyfile(archive,snapshot)
                    recovery_requests=[r for r in requests if r.get('phase')=='recovery']
                    require(bool(recovery_requests),'No recovery wire request')
                    recovery_body=read(case,recovery_requests[0]['body_path']);rm,_=wire_view(recovery_body,p)
                    user=[m for m in rm if m.get('role')=='user'][-1]
                    command_text=text(user.get('content',[]));require(command_text.startswith('RESTORE '),'Recovery prompt missing')
                    args=strict_json_loads(command_text.removeprefix('RESTORE '))
                    require(set(args)=={'handle','start','chars'} and type(args['start']) is int and args['start']>=0 and type(args['chars']) is int and 1<=args['chars']<=6000,'Recovery span invalid')
                    with readonly_db(snapshot,immutable_snapshot=True) as adb:
                        blob=adb.execute('SELECT content FROM blobs WHERE namespace=? AND handle=?',('offline_boundary',args['handle'])).fetchone()
                    require(blob is not None and digest(blob['content'])==args['handle'],'Requested recovery blob missing or corrupt')
                    content=blob['content'];span=content[args['start']:args['start']+args['chars']]
                    require(MARKER in span,'Probe does not cover the deliberately removed marker')
                    probe=RecoveryProbe(args['handle'],args['start'],args['chars'],len(content),span)
                rec=verify_recovery(rpc,probe)
                compact_index=next((i for i,e in enumerate(rpc) if e.get('type')=='response' and e.get('command')=='compact' and e.get('success') is True),None)
                followup_index=next((i for i,e in enumerate(rpc) if e.get('type')=='message_start' and e.get('message',{}).get('role')=='user' and text(e['message'].get('content',[]))=='FOLLOWUP: confirm continuation using the current context.'),None)
                require(compact_index is not None and followup_index is not None and compact_index<followup_index,'Continuation did not occur after compact response')
                require(rec==row.get('recovery'),'Recovery report disagrees with actual events')
            else:require(not ops and state[0]['memory'] is None,'Control unexpectedly attempted compaction')
        verified.append({'protocol':p,'condition':c,'pressure':s,'actual_commit':activated,
            'expected_rejection':rejected,'followup_bytes':row.get('followup',{}).get('wire_content_bytes')})
    for p in report['protocols']:
        rows={x['condition']:x for x in report['rows'] if x['protocol']==p and x['pressure']=='long'}
        require(rows['tools_no_compact']['followup']['tool_names']==rows['mask_checkpoint']['followup']['tool_names'],'Tool matching failed')
        tools=[]
        for condition in ('tools_no_compact','mask_checkpoint'):
            item=rows[condition]
            body=read(root/item['case_dir'],item['followup']['body_path'])
            tools.append(body.get('tools'))
        require(tools[0]==tools[1],'Tool schema matching failed')
        require(rows['mask_checkpoint']['followup']['wire_content_bytes']<rows['tools_no_compact']['followup']['wire_content_bytes'],'Representation not smaller')
    return {'schema':1,'kind':'independent_local_artifact_checks','status':'passed','cells':len(expected),
        'actual_commits':sum(x['actual_commit'] for x in verified),
        'expected_capacity_rejections':sum(x['expected_rejection'] for x in verified),
        'paid_model_calls':0,'natural_trigger_verified':False,'agent_quality_measured':False,
        'cost_saving_measured':False,'independent_model_review_passed':False,
        'rows':verified}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    a=p.parse_args();require(not a.out.exists(),'New audit output required')
    result=audit(a.run);atomic_write(a.out,canonical(result));print(canonical(result));return 0
if __name__=='__main__':raise SystemExit(main())
