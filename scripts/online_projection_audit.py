#!/usr/bin/env python3
"""Independently check owned RC6 wire/session/archive evidence (no model, no server).

This checker certifies one scripted interface profile, never an effect estimate,
real-provider acceptance or a formal research gate. Hashes are corruption checks,
not signatures against an operator who can consistently replace the whole bundle.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from reverpi.config import Provider,StudyConfig
from reverpi.protocols import Message,build_request,validate_messages
from reverpi.research_audit import readonly_db
from reverpi.util import canonical,digest,strict_json_loads,unseal_cache,atomic_write,contained_regular_file

MARKER='HISTORICAL_MARKER_9baf4477219db8809eec246607c77954'
CONDITIONS=('pi_original','tools_observe','online_apply')
PROTOCOLS=('chat_completions','responses')
PREFIX='[reverpi-observation-v1 '


def require(ok:bool,message:str):
    if not ok:raise ValueError(message)


def load(base:Path,path:str):
    return strict_json_loads(contained_regular_file(base,path).read_bytes())


def db_path(case:Path,name:str)->Path:
    path=contained_regular_file(case,'gateway/'+name)
    for suffix in ('-wal','-journal','-shm'):
        side=Path(str(path)+suffix)
        if side.exists() or side.is_symlink():
            require(not side.is_symlink() and side.is_file(),'Unsafe database sidecar')
            if suffix!='-shm':require(side.stat().st_size==0,'Audit requires a stopped, checkpointed database')
    return path


def transcript(case:Path):
    entries=[strict_json_loads(line) for line in contained_regular_file(case,'session.jsonl').read_text().splitlines() if line.strip()]
    require(not any(x.get('type')=='compaction' for x in entries),'Native compaction is a different mechanism')
    results={};calls={}
    for entry in entries:
        m=entry.get('message',{})
        if m.get('role')=='assistant':
            for block in m.get('content',[]):
                if block.get('type')=='toolCall':calls[block['id']]=block
        if m.get('role')=='toolResult':
            require(m['toolCallId'] not in results,'Repeated tool result in original session')
            require(all(b.get('type')=='text' for b in m['content']),'Unexpected non-text owned result')
            results[m['toolCallId']]={'text':'\n'.join(b['text'] for b in m['content']),
                                     'is_error':m.get('isError') is not False,'tool_name':m['toolName']}
    return results,calls


def check_record(record:dict,full:dict,packed:set,original_results:dict,blobs:dict)->None:
    """Reference gate evaluation; does not invoke the projection implementation."""
    require(record.get('kind')=='online_observation_projection_v1' and record.get('schema')==1,'Unknown trace contract')
    require(record.get('provider_tokens_measured') is False and record.get('native_compaction_commit') is False,'Trace overclaims')
    source=record['source_messages'];sent=record['sent_messages'];cfg=record['config'];mode=record['mode']
    require(mode in {'observe','apply'} and cfg['mode']==mode,'Unexpected projection mode')
    validate_messages([Message.from_dict(m) for m in source]);validate_messages([Message.from_dict(m) for m in sent])
    require(len(source)==len(sent),'Message removed or added')
    require(digest(source)==record['source_sha'] and digest(sent)==record['sent_sha'],'Trace hash mismatch')
    require(record['source_utf8_bytes']==len(canonical(source).encode()) and record['sent_utf8_bytes']==len(canonical(sent).encode()),'Byte count mismatch')
    decisions=record['observations'];expected_indices=[i for i,m in enumerate(source) if m['role']=='tool']
    require([d['index'] for d in decisions]==expected_indices,'Observation coverage/order mismatch')
    recent={source[i]['call_id'] for i in expected_indices[-cfg['keep_recent_results']:]};by_index={d['index']:d for d in decisions}
    for i,(a,b) in enumerate(zip(source,sent)):
        if a['role']!='tool':
            require(a==b,'Non-observation content, calls or reasoning changed');continue
        d=by_index[i];ident=a['call_id'];raw=original_results.get(ident)
        require(raw is not None and raw['text']==a['content'],'Source does not match immutable Pi result')
        require(d['call_id']==ident and d['tool_name']==raw['tool_name'] and d['is_error'] is raw['is_error'],'Metadata disagreement')
        require(d['content_sha']==digest(a['content']),'Wrong archive identity')
        require(blobs.get(d['content_sha'])==a['content'],'Archive missing or corrupt')
        require(d['full_sends_before']==full.get(ident,0) and d['packed_before'] is (ident in packed),'Exposure state mismatch')
        meta={k:d[k] for k in ('call_id','tool_name','content_sha','is_error')}
        require(d['meta_sha']==digest(meta),'Metadata hash mismatch')
        size=len(a['content'].encode());expected=False
        if raw['is_error']:reason='error_or_unknown_status'
        elif raw['tool_name'] in {'recover_evidence','search_evidence','revalidate_evidence'}:reason='recovery_or_current_verification'
        elif a['content'].startswith(PREFIX):reason='already_a_receipt'
        elif size<cfg['min_observation_bytes']:reason='small_observation'
        elif ident not in packed and ident in recent:reason='recent_result'
        elif ident not in packed and full.get(ident,0)<cfg['full_exposures']:reason='first_full_exposures'
        else:reason='already_projected' if ident in packed else 'aged_observation';expected=True
        # Owned fixture receipts are guaranteed smaller; all nonpositive cases are
        # separately unit-tested in the implementation's general contract suite.
        require(d['eligible'] is expected and d['reason']==reason,'Eligibility reason mismatch')
        applied=expected and mode=='apply';require(d['applied'] is applied,'Control or treatment mismatch')
        require({k:v for k,v in a.items() if k!='content'}=={k:v for k,v in b.items() if k!='content'},'Tool envelope changed')
        require(d['original_bytes']==size and d['projected_bytes']==len(b['content'].encode()),'Observation byte count mismatch')
        if applied:
            header,rest=b['content'].split('\n',1)
            require(header.startswith(PREFIX) and header.endswith(']'),'Missing receipt')
            info=strict_json_loads(header[len(PREFIX):-1]);require(info=={'handle':d['content_sha'],'tool':d['tool_name'],
                'original_bytes':size,'total_chars':len(a['content']),'offset_unit':'unicode_codepoints'},'Receipt metadata mismatch')
            half=cfg['excerpt_bytes']//2;encoded=a['content'].encode()
            head=encoded[:half].decode('utf-8',errors='ignore');tail=encoded[-(cfg['excerpt_bytes']-half):].decode('utf-8',errors='ignore')
            recovery_line = ('Use recover_evidence with this handle and start/chars for exact text, or literal query to locate a span.\n'
                if cfg.get('recovery_interface','legacy') == 'legacy' else
                'Read exact historical text with recover_evidence(handle, start, chars). Locate a span with search_evidence(query, chars); use returned handle and start. Never send query to recover_evidence.\n')
            expected_rest=('Historical output omitted from this request, not deleted. This is not a current-state verification.\n'
                + recovery_line + '[head]\n'+head+'\n[middle omitted]\n'+tail+'\n[tail]')
            require(rest==expected_rest and len(b['content'].encode())<size,'Receipt text is not the bounded exact representation')
            packed.add(ident)
        else:
            require(a==b,'Unselected observation changed');full[ident]=full.get(ident,0)+1
    require(record['applied_count']==sum(d['applied'] for d in decisions) and record['eligible_count']==sum(d['eligible'] for d in decisions),'Trace counts disagree')


def audit(root:Path)->dict:
    root=root.resolve(strict=True);r=load(root,'report.json')
    require(r.get('kind')=='owned_automatic_online_projection_v1' and r.get('status')=='passed','Incomplete owned profile')
    require(r.get('real_model_calls')==0 and r.get('scripted_actor') is True and r.get('native_gate_eligible') is False
        and r.get('quality_measured') is False and r.get('token_savings_measured') is False,'Overstated experiment authority')
    protocols=r['protocols'];require(protocols and len(set(protocols))==len(protocols) and set(protocols)<=set(PROTOCOLS),'Protocol set invalid')
    expect={(p,c,s) for p in protocols for c in CONDITIONS for s in ('short','long')}
    rows=r['rows'];require(len(rows)==len(expect) and {(x['protocol'],x['condition'],x['pressure']) for x in rows}==expect,'Missing or duplicate cases')
    output=[];toolsets={};source_fixtures={}
    for row in rows:
        p,c,s=row['protocol'],row['condition'],row['pressure'];name=f'{p}__{c}__{s}'
        require(row['case_dir']==name,'Untrusted case path');case=root/name
        require(load(case,'report.json')==row and row['status']=='passed' and row['manual_compact_requested'] is False,'Case mismatch')
        require(row['session_revoked'] is True and row['gateway_stopped'] is True and row['native_gate_eligible'] is False,'Lifecycle or authority mismatch')
        st=load(case,'agent-config/settings.json');require(st==row['settings'] and st['compaction']=={'enabled':True,'keepRecentTokens':20000,'reserveTokens':66560},'Original native settings changed')
        provider=Provider.model_validate(load(case,'provider.json'));study=StudyConfig.model_validate(load(case,'study.json'))
        mode={'pi_original':'off','tools_observe':'observe','online_apply':'apply'}[c]
        require(study.online_projection.mode==mode and provider.mock and provider.model=='mock-reasoner' and provider.protocol==p,'Fixture route changed')
        require(provider.context_window==131072 and provider.effort=='low' and provider.max_output_tokens==65536,'Provider bounds changed')
        raw_results,calls=transcript(case)
        require(len([x for x in calls.values() if x['name']=='read'])==5,'Missing actual reads')
        require(any(MARKER in x['text'] for x in raw_results.values()),'Original marker lost')
        rpc=[strict_json_loads(x) for x in contained_regular_file(case,'rpc.jsonl').read_text().splitlines() if x.strip()]
        require(sum(x.get('type')=='response' and x.get('command')=='prompt' and x.get('success') is True for x in rpc)==1,'Not a single prompt')
        require(not any(x.get('command')=='compact' or x.get('type')=='compaction_start' for x in rpc),'Manual/native compaction occurred')
        with readonly_db(db_path(case,'sessions.sqlite'),immutable_snapshot=True) as db:
            ss=db.execute('select * from sessions').fetchall();ops=db.execute('select * from compact_ops').fetchall()
        require(len(ss)==1 and ss[0]['disabled']==1 and ss[0]['revision']==0 and not ops,'Unexpected native/custom commit or open session')
        blobs={}
        with readonly_db(db_path(case,'archive.sqlite'),immutable_snapshot=True) as db:
            for blob in db.execute('select * from blobs where namespace=?',('owned_online',)):
                require(digest(blob['content'])==blob['handle'],'Corrupt archived content');blobs[blob['handle']]=blob['content']
        requests=load(case,'requests.json');require(requests==row['requests'] and row['request_count']==len(requests),'Request index differs')
        events=load(case,'projection_events.json')
        if mode!='off':
            with readonly_db(db_path(case,'projection.sqlite'),immutable_snapshot=True) as db:
                actual=[{'seq':e['seq'],'state':e['state'],'record':unseal_cache(e['record'])} for e in db.execute('select * from projection_events order by seq')]
            require(events==actual and len(events)==len(requests) and all(e['state']=='complete' for e in events),'Projection export/DB differs')
        else:require(not events and not (case/'gateway/projection.sqlite').exists(),'Disabled control created online trace')
        full={};packed=set();applied=0;definitions=None
        for i,q in enumerate(requests):
            require(q['index']==i and q['path']==f'wire_{i:03d}.json','Wire filename/index differs');body=load(case,q['path'])
            require(digest(body)==q['body_sha256'],'Wire hash differs')
            definitions=body.get('tools',[])
            tools=[x['function'] for x in definitions] if p=='chat_completions' else [{k:x[k] for k in ('name','description','parameters')} for x in definitions]
            if mode!='off':
                e=events[i]['record'];require({"recovery_interface":"legacy",**e['config']}==study.online_projection.model_dump(),'Trace policy drift')
                check_record(e,full,packed,raw_results,blobs)
                _,expected_body=build_request(provider,[Message.from_dict(m) for m in e['sent_messages']],tools)
                require(body==expected_body,'Actual provider wire differs from projected canonical request');applied+=e['applied_count']>0
        should=c=='online_apply' and s=='long'
        require((applied>0) is should and row['applied_requests']==applied,'Activation control mismatch')
        require(len(requests)==(8 if should else 6),'Unexpected fixed scripted requests')
        exact=[v for v in calls.values() if v['name']=='recover_evidence' and 'handle' in v['arguments']]
        if should:
            require(len(exact)==1,'Missing/extra exact recovery');call=exact[0];args=call['arguments']
            require(set(args)=={'handle','start','chars'} and args['chars']==len(MARKER),'Recovery span differs')
            content=blobs.get(args['handle']);require(content is not None and content[args['start']:args['start']+args['chars']]==MARKER,'Recovery did not target removed content')
            result=strict_json_loads(raw_results[call['id']]['text']);require(result.get('text')==MARKER and result.get('handle')==args['handle'],'Actual recovery tool result differs')
        else:require(not exact,'No-op control unexpectedly recovered')
        toolsets[(p,c,s)]=definitions
        source_fixtures[(p,c,s)]=[contained_regular_file(case,f'workspace/page_{i:02d}.txt').read_bytes() for i in range(5)]
        output.append({'protocol':p,'condition':c,'pressure':s,'requests':len(requests),'projection_requests':applied,
                       'exact_recovery':should,'native_compaction_commits':0,'source_history_preserved':True})
    for p in protocols:
        for s in ('short','long'):
            require(toolsets[(p,'tools_observe',s)]==toolsets[(p,'online_apply',s)],'Tool schemas differ across matched arms')
            require(source_fixtures[(p,'pi_original',s)]==source_fixtures[(p,'tools_observe',s)]==source_fixtures[(p,'online_apply',s)],'Workspace fixtures differ')
    return {'schema':1,'status':'passed','source_sha':r['source_sha'],'rows':output,'cells':len(output),
            'automatic_request_projection_verified':True,'scripted_actor_only':True,'manual_compact_rpc_calls':0,
            'real_model_quality_verified':False,'paid_authorized':False,'provider_tokens_or_money_saving_measured':False}


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--run',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args()
    require(not a.out.exists() and not a.out.is_symlink(),'Use a new audit output');result=audit(a.run);atomic_write(a.out,canonical(result));print(canonical({'status':result['status'],'cells':result['cells']}));return 0
if __name__=='__main__':raise SystemExit(main())
