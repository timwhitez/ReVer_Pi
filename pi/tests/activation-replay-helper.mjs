import fs from 'node:fs';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {pathToFileURL,fileURLToPath} from 'node:url';
export const hash=b=>createHash('sha256').update(b).digest('hex');
export function replayEntries(entries,engine,settings,window){
 if(!Array.isArray(entries)||!entries.length||entries[0].type!=='session')throw Error('Session header required');
 if(!Number.isSafeInteger(window)||!Number.isSafeInteger(settings.reserveTokens)||!Number.isSafeInteger(settings.keepRecentTokens)
     ||settings.keepRecentTokens<1||settings.reserveTokens<1||settings.reserveTokens>=window)throw Error('Bad context settings');
 const ids=new Set();
 const pathEntries=[];const checks=[];
 for(const e of entries){
  if(e.type==='session'){if(e!==entries[0])throw Error('Multiple session headers');continue;}
  if(typeof e.id!=='string'||ids.has(e.id))throw Error('Missing/duplicate session entry ID');
  if(pathEntries.length ? e.parentId!==pathEntries.at(-1).id : e.parentId!==null && e.parentId!==undefined)throw Error('Nonlinear/forked history: explicit branch replay required');
  ids.add(e.id);pathEntries.push(e);
  if(e.type!=='message'||e.message?.role!=='assistant')continue;
  const m=e.message;
  if(!m.usage||['input','output','cacheRead','cacheWrite'].some(k=>!Number.isFinite(m.usage[k])||m.usage[k]<0))throw Error('Malformed usage');
  if(m.usage.totalTokens!==undefined && (!Number.isFinite(m.usage.totalTokens)||m.usage.totalTokens<0))throw Error('Malformed total usage');
  const tokens=engine.calculateContextTokens(m.usage);
  const prepared=engine.prepareCompaction(pathEntries,settings);
  const visible=pathEntries.filter(x=>x.type==='message').reduce((n,x)=>n+engine.estimateTokens(x.message),0);
  checks.push({entry_id:e.id,stop_reason:m.stopReason,reported_usage_tokens:tokens,
    visible_estimate_tokens:visible,threshold_crossed:engine.shouldCompact(tokens,window,settings),
    legal_compaction_preparation:prepared!==undefined,
    archived_messages_if_prepared:prepared ? prepared.messagesToSummarize.length+prepared.turnPrefixMessages.length : 0});
 }
 return {assistant_prefixes:checks.length,threshold_crossings:checks.filter(x=>x.threshold_crossed).length,
  preparable_prefixes:checks.filter(x=>x.legal_compaction_preparation).length,
  jointly_eligible_prefixes:checks.filter(x=>x.threshold_crossed&&x.legal_compaction_preparation).length,
  peak_usage_tokens:Math.max(0,...checks.map(x=>x.reported_usage_tokens)),
  peak_visible_estimate_tokens:Math.max(0,...checks.map(x=>x.visible_estimate_tokens)),checks};
}
export async function run(supplement,audit,out){
 supplement=path.resolve(supplement);out=path.resolve(out);
 if(out===supplement||out.startsWith(supplement+path.sep))throw Error('Output must not modify the supplement');
 const report=JSON.parse(fs.readFileSync(audit,'utf8'));
 if(report.kind!=='supplement_static_audit'||report.manifest_files_verified!==213||report.official_results_verified!==48)throw Error('Verified TB24 supplement audit required');
 const packageRoot=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../node_modules/@earendil-works/pi-coding-agent');
 const packageInfo=JSON.parse(fs.readFileSync(path.join(packageRoot,'package.json'),'utf8'));
 if(packageInfo.version!=='0.84.2')throw Error('This replay is pinned to Pi 0.84.2; do not substitute another version');
 const enginePath=path.join(packageRoot,'dist/core/compaction/compaction.js');
 const engineHash=hash(fs.readFileSync(enginePath));
 // A fixed code hash guards against altered dependencies carrying the same version.
 if(engineHash!=='fcb12f1eb4d38578978e1a8e3e382a3fccfd5e0ccf87bc86979a9a8d9c145c7b')throw Error('Pinned Pi compaction implementation differs');
 const engine=await import(pathToFileURL(enginePath));
 const manifest=JSON.parse(fs.readFileSync(path.join(supplement,'SUPPLEMENT_MANIFEST.json'),'utf8'));
 if(hash(fs.readFileSync(path.join(supplement,'SUPPLEMENT_MANIFEST.json')))!==report.manifest_sha256)throw Error('Audit/supplement manifest mismatch');
 const files=new Map(manifest.map(x=>[x.path,x])); const rows=[];const retentionGrid=[20000,12000,8000,4000];
 for(const cell of report.cells){
  if(!cell.session_log){rows.push({cell:cell.cell,method:cell.method,task:cell.task,status:'MISSING'});continue;}
  const name=cell.session_log;
  if(!/^session-logs\/[A-Za-z0-9_.-]+_session\.jsonl$/.test(name)||!files.has(name))throw Error('Unregistered log');
  const f=path.join(supplement,name);const real=fs.realpathSync(f);
  if(real!==path.resolve(f)||!fs.lstatSync(f).isFile())throw Error('Nonregular log');
  const bytes=fs.readFileSync(f);
  if(hash(bytes)!==files.get(name).sha256)throw Error('Log changed');
  const entries=bytes.toString('utf8').split('\n').filter(x=>x.trim()).map(JSON.parse);
  rows.push({cell:cell.cell,task:cell.task,method:cell.method,status:'REPLAYED',session_sha256:hash(bytes),
   ...replayEntries(entries,engine,report.reconstructed_launcher_settings,report.frozen_provider_public_fields.context_window),
   exploratory_retention_sensitivity:retentionGrid.map(keepRecentTokens=>{
     const x=replayEntries(entries,engine,{...report.reconstructed_launcher_settings,keepRecentTokens},report.frozen_provider_public_fields.context_window);
     return {keepRecentTokens,preparable_prefixes:x.preparable_prefixes,jointly_eligible_prefixes:x.jointly_eligible_prefixes};
   })});
 }
 const available=rows.filter(x=>x.status==='REPLAYED');
 const result={schema:1,kind:'pinned_pi_counterfactual_preparation_replay',pi_version:packageInfo.version,
  pi_compaction_js_sha256:engineHash,settings:report.reconstructed_launcher_settings,
  settings_are_reconstructed_not_captured:true,available_sessions:available.length,missing_sessions:rows.length-available.length,
  assistant_prefixes:available.reduce((a,x)=>a+x.assistant_prefixes,0),
  threshold_crossings:available.reduce((a,x)=>a+x.threshold_crossings,0),
  preparable_prefixes:available.reduce((a,x)=>a+x.preparable_prefixes,0),
  jointly_eligible_prefixes:available.reduce((a,x)=>a+x.jointly_eligible_prefixes,0),
  new_model_calls:0,rows,
  exploratory_retention_sensitivity:retentionGrid.map((keepRecentTokens,i)=>({keepRecentTokens,
    sessions_with_a_preparable_prefix:available.filter(x=>x.exploratory_retention_sensitivity[i].preparable_prefixes>0).length,
    preparable_prefixes:available.reduce((a,x)=>a+x.exploratory_retention_sensitivity[i].preparable_prefixes,0),
    jointly_eligible_prefixes:available.reduce((a,x)=>a+x.exploratory_retention_sensitivity[i].jointly_eligible_prefixes,0),
    deployment_recommendation:false,model_quality_evaluated:false})),
  limitations:['Prefix checks are offline counterfactuals, not historical hook invocations.',
    'Pinned Pi checks compaction before prompt and after agent run, not after each tool turn.',
    'Missing logs remain unknown; no hypothetical replay estimates task reward or token saving.',
    'Visible char estimates exclude some opaque provider state; not an exact tokenizer.']};
 fs.writeFileSync(out,JSON.stringify(result,null,2)+'\n',{flag:'wx',mode:0o600});return result;
}
