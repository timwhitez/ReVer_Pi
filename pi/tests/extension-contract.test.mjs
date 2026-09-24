/** Contract double only: this does NOT certify the installed upstream Pi API. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);const ts=require('typescript');
const source=await readFile(new URL('../src/index.ts',import.meta.url),'utf8');
const harnessPrelude=`
const convertToLlm=(m)=>m;
const Type={Object:(x)=>x,Optional:(x)=>x,String:()=>({}),Integer:(x)=>x};
const createAssistantMessageEventStream=()=>{let end;const s={events:[],finished:new Promise(r=>end=r),push(e){this.events.push(e)},end(){end()}};return s;};
`;
const transformed=source.replace(/import type \{ ExtensionAPI \} from "@earendil-works\/pi-coding-agent";\n/,'')
 .replace(/import \{ convertToLlm \} from "@earendil-works\/pi-coding-agent";\n/,'')
 .replace(/import \{ createAssistantMessageEventStream, type AssistantMessage \} from "@earendil-works\/pi-ai";\n/,'')
 .replace(/import \{ Type \} from "typebox";\n/,'')
 .replace('"./core.ts"',JSON.stringify(new URL('../src/core.ts',import.meta.url).href))
 .replace('"./revalidation-extension.ts"',JSON.stringify(new URL('../src/revalidation-extension.ts',import.meta.url).href));
const code=ts.transpileModule(harnessPrelude+transformed,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
const extension=(await import('data:text/javascript;base64,'+Buffer.from(code).toString('base64'))).default;
async function setup(method='rever_lite', extra={}, responder){
 const envKeys=['REVER_SESSION_TOKEN','REVER_GATEWAY_URL','REVER_MAX_TOOLS','REVER_MAX_TURNS'];
 const savedEnv=Object.fromEntries(envKeys.map(k=>[k,process.env[k]]));
 const restoreEnv=()=>{for(const k of envKeys){if(savedEnv[k]===undefined)delete process.env[k];else process.env[k]=savedEnv[k];}};
 process.env.REVER_SESSION_TOKEN='scoped-test-token';process.env.REVER_GATEWAY_URL='http://127.0.0.1:8765';
 process.env.REVER_MAX_TOOLS='10';process.env.REVER_MAX_TURNS='20';
 const calls=[],hooks={},tools=[];let provider;
 const profile={method,recovery:{max_chars:6000,max_calls:3},revision:0,accounting:{attempts:0},model:'reasoner',effort:'low',context_window:131072,max_output_tokens:4096,request_deadline_ms:10000,
 prices:{input_per_million:1,output_per_million:2,cached_input_per_million:0.2},...extra};
 const original=globalThis.fetch;
 globalThis.fetch=async(url,options)=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return responder&&url.endsWith('/compact')?responder():new Response(JSON.stringify(url.endsWith('/session')?profile:{text:'answer',calls:[],reasoning:'kept',response_items:null,model:'reasoner',response_id:'r1',usage:{input_tokens:10,output_tokens:5}}));};
 const pi={registerProvider(n,p){provider=p;},registerTool(t){tools.push(t);},on(n,fn){hooks[n]=fn;}};
 try{await extension(pi);}catch(e){globalThis.fetch=original;restoreEnv();throw e;}
 return {calls,hooks,tools,provider,restore(){globalThis.fetch=original;restoreEnv();}};
}
test('pi_original adds no recovery tool',async()=>{const h=await setup('pi_original');try{assert.equal(h.tools.length,0);assert.equal(await h.hooks.session_before_compact({},{}),undefined);}finally{h.restore();}});
test('capability-matched native baseline has recovery, native compaction unchanged',async()=>{const h=await setup('pi_native');try{assert.equal(h.tools[0].name,'recover_evidence');assert.equal(await h.hooks.session_before_compact({},{}),undefined);}finally{h.restore();}});
test('parallel read cannot acquire later mutation version',async()=>{const h=await setup();try{const ctx={abort(){}};await h.hooks.tool_call({toolCallId:'r',toolName:'read'},ctx);await h.hooks.tool_call({toolCallId:'w',toolName:'edit'},ctx);const r=await h.hooks.tool_result({toolCallId:'r',toolName:'read'});assert.equal(r.details.rever_observation.dependencies.workspace_epoch,'0');assert.equal(r.details.rever_observation.updates.workspace_epoch,'1');}finally{h.restore();}});
test('missing dispatch is unknown, never current by assumption',async()=>{const h=await setup();try{const r=await h.hooks.tool_result({toolCallId:'unknown',toolName:'bash',input:{command:'pytest'}});assert.equal(r.details.rever_observation.dependencies.workspace_epoch,'UNKNOWN');}finally{h.restore();}});
test('no metadata change on pi_original',async()=>{const h=await setup('pi_original');try{assert.equal(await h.hooks.tool_result({toolCallId:'x',toolName:'read'}),undefined);}finally{h.restore();}});
test('reusing a session with prior paid attempts is blocked',async()=>{await assert.rejects(setup('rever_lite',{accounting:{attempts:1}}),/Fresh task/);});
test('custom compaction commit maps exact Pi boundary',async()=>{const h=await setup('rever_lite',{},()=>new Response(JSON.stringify({revision:1,memory:{text:'committed',input_sha:'x'}})));try{const a=await h.hooks.session_before_compact({preparation:{messagesToSummarize:[{role:'user',content:'task'}],turnPrefixMessages:[],firstKeptEntryId:'boundary',tokensBefore:1000},signal:new AbortController().signal},{abort(){throw Error('not expected')},hasUI:false});assert.equal(a.compaction.summary,'committed');assert.equal(a.compaction.firstKeptEntryId,'boundary');}finally{h.restore();}});
test('failed compaction cancels and stops, never substitutes native algorithm',async()=>{const h=await setup('rever_lite',{},()=>new Response(JSON.stringify({error:{kind:'empty_output'}}),{status:422}));try{let aborted=0;const a=await h.hooks.session_before_compact({preparation:{messagesToSummarize:[{role:'user',content:'task'}],turnPrefixMessages:[],firstKeptEntryId:'b',tokensBefore:1},signal:new AbortController().signal},{abort(){aborted++},hasUI:false});assert.deepEqual(a,{cancel:true});assert.equal(aborted,1);assert.equal(h.calls.filter(c=>c.url.endsWith('/compact')).length,1);}finally{h.restore();}});
test('gateway response translated as complete events with durable envelope',async()=>{const h=await setup();try{const model={api:'rever-gateway-v1',provider:'rever-gateway',id:'reasoner'};const stream=h.provider.streamSimple(model,{messages:[{role:'user',content:'question'}]},{});await stream.finished;const done=stream.events.at(-1);assert.equal(done.type,'done');assert.equal(done.message.content[0].redacted,true);assert.equal(h.calls.at(-1).body.effort,'low');}finally{h.restore();}});
test('changed reasoning effort emits terminal error before paid request',async()=>{const h=await setup();try{const stream=h.provider.streamSimple({api:'rever-gateway-v1',provider:'rever-gateway',id:'reasoner'},{messages:[{role:'user',content:'valid question'}]},{reasoning:'high'});await stream.finished;assert.equal(stream.events.at(-1).type,'error');assert.equal(h.calls.length,1);}finally{h.restore();}});

// v1.2 server-advertised tool capabilities, not a hard-coded 6000-char ceiling.
test('v1.2 tool schema uses frozen gateway recovery quota',async()=>{const h=await setup('mask',{recovery:{max_chars:999,max_calls:2}});try{assert.equal(h.tools[0].parameters.chars.maximum,999);}finally{h.restore();}});
test('v1.2 zero recovery quota exposes no unusable tool',async()=>{const h=await setup('mask',{recovery:{max_chars:999,max_calls:0}});try{assert.equal(h.tools.length,0);}finally{h.restore();}});
for(const recovery of [undefined,{max_chars:0,max_calls:2},{max_chars:999,max_calls:-1},{max_chars:999,max_calls:'2'}])
 test('v1.2 invalid recovery capability fails before provider registration '+JSON.stringify(recovery),async()=>{await assert.rejects(setup('mask',{recovery}),/recovery/i);});

test('harness restores environment after success and rejection',async()=>{const keys=['REVER_SESSION_TOKEN','REVER_GATEWAY_URL','REVER_MAX_TOOLS','REVER_MAX_TURNS'];const before=keys.map(k=>process.env[k]);const h=await setup();h.restore();assert.deepEqual(keys.map(k=>process.env[k]),before);await assert.rejects(setup('mask',{recovery:undefined}));assert.deepEqual(keys.map(k=>process.env[k]),before);});
test('error inside a 200 body (early response headers) is a terminal failure',async()=>{
 const envKeys=['REVER_SESSION_TOKEN','REVER_GATEWAY_URL','REVER_MAX_TOOLS','REVER_MAX_TURNS'];
 const saved=Object.fromEntries(envKeys.map(k=>[k,process.env[k]]));
 process.env.REVER_SESSION_TOKEN='t';process.env.REVER_MAX_TOOLS='10';process.env.REVER_MAX_TURNS='20';
 const profile={method:'rever_lite',recovery:{max_chars:6000,max_calls:3},revision:0,accounting:{attempts:0},model:'reasoner',effort:'low',context_window:131072,max_output_tokens:4096,request_deadline_ms:10000,prices:{input_per_million:1,output_per_million:2,cached_input_per_million:.2}};
 const original=globalThis.fetch;
 globalThis.fetch=async(url)=>url.endsWith('/session')?new Response(JSON.stringify(profile)):new Response(JSON.stringify({error:{kind:'provider_cooldown',message:'cited'}}),{status:200});
 let provider;const pi={registerProvider(n,p){provider=p;},registerTool(){},on(){}};
 try{
   await extension(pi);
   const stream=provider.streamSimple({api:'rever-gateway-v1',provider:'rever-gateway',id:'reasoner'},{messages:[{role:'user',content:'q'}]},{});
   await stream.finished;
   const err=stream.events.at(-1);
   assert.equal(err.type,'error');
   assert.match(err.error.errorMessage,/provider_cooldown/);
 }finally{globalThis.fetch=original;for(const k of envKeys){if(saved[k]===undefined)delete process.env[k];else process.env[k]=saved[k];}}
});

test('RC4 presence of a null error field is not accepted as success',async()=>{
 const h=await setup('rever_lite',{},()=>new Response(JSON.stringify({error:null,revision:1,memory:{text:'not accepted'}})));
 try{let aborted=0;const r=await h.hooks.session_before_compact({preparation:{messagesToSummarize:[{role:'user',content:'task'}],turnPrefixMessages:[],firstKeptEntryId:'b',tokensBefore:1},signal:new AbortController().signal},{abort(){aborted++},hasUI:false});
 assert.deepEqual(r,{cancel:true});assert.equal(aborted,1);
 }finally{h.restore();}
});

// RC7.1: explicit split tool ABI, legacy branch remains unchanged.
test('split recovery advertises one purpose per tool',async()=>{
 const h=await setup('mask',{recovery:{interface:'split_v1',max_chars:999,max_calls:3}});
 try{
  assert.deepEqual(h.tools.map(t=>t.name),['recover_evidence','search_evidence']);
  assert.equal('query' in h.tools[0].parameters,false);
  assert.equal('handle' in h.tools[1].parameters,false);
  assert.equal(h.tools[0].parameters.chars.maximum,999);
 }finally{h.restore();}
});
for(const [name,args] of [
 ['recover_evidence',{handle:'a'.repeat(64),query:'key'}],
 ['recover_evidence',{}],['recover_evidence',{handle:'../file'}],
 ['recover_evidence',{handle:'a'.repeat(64),start:-1}],
 ['recover_evidence',{handle:'a'.repeat(64),chars:0}],
 ['recover_evidence',{handle:'a'.repeat(64),chars:1000}],
 ['search_evidence',{query:'x',handle:'a'.repeat(64)}],
 ['search_evidence',{query:''}],['search_evidence',{query:'x'.repeat(257)}],
 ['search_evidence',{query:'x',start:1}],['search_evidence',{query:'x',chars:true}]
]) test('split rejects invalid arguments without HTTP '+name+' '+JSON.stringify(args),async()=>{
 const h=await setup('mask',{recovery:{interface:'split_v1',max_chars:999,max_calls:3}});
 try{const n=h.calls.length;const r=await h.tools.find(t=>t.name===name).execute('bad',args);
  assert.equal(r.isError,true);assert.match(r.content[0].text,/recovery_arguments/);assert.equal(h.calls.length,n);
 }finally{h.restore();}
});
test('split valid read and search share server quota route, default fits quota',async()=>{
 const h=await setup('mask',{recovery:{interface:'split_v1',max_chars:999,max_calls:3}});
 try{
  await h.tools[0].execute('r',{handle:'a'.repeat(64)});
  assert.equal(h.calls.at(-1).body.chars,999);assert.equal('query' in h.calls.at(-1).body,false);
  await h.tools[1].execute('s',{query:'current_word'});
  assert.ok(h.calls.at(-1).url.endsWith('/recover'));assert.equal('handle' in h.calls.at(-1).body,false);
  assert.equal(h.calls.at(-1).body.query,'current_word');
 }finally{h.restore();}
});
test('split search result stays a historical observation, not a workspace edit',async()=>{
 const h=await setup('mask',{recovery:{interface:'split_v1',max_chars:6000,max_calls:3}});
 try{const ctx={abort(){}};
 await h.hooks.tool_call({toolCallId:'s',toolName:'search_evidence'},ctx);
 assert.equal(await h.hooks.tool_result({toolCallId:'s',toolName:'search_evidence'}),undefined);
 await h.hooks.tool_call({toolCallId:'r',toolName:'read'},ctx);
 const v=await h.hooks.tool_result({toolCallId:'r',toolName:'read'});
 assert.equal(v.details.rever_observation.updates.workspace_epoch,'0');
 }finally{h.restore();}
});
test('unknown recovery interface is rejected',async()=>{
 await assert.rejects(setup('mask',{recovery:{interface:'made_up',max_chars:6000,max_calls:3}}),/Unknown recovery interface/);
});
