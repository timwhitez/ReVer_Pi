/** Tool failures must reach the REAL pinned Pi agent loop as isError:true (issue #10).
 * The gateway is a local fetch double and the model is a scripted stream: no
 * Provider request, no network, no model output is evidence of anything. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {runToolThroughPi} from './pi-loop-helper.mjs';
import extension from '../src/index.ts';

const HANDLE='a'.repeat(64);

async function withExtension(recovery,recoverResponse,fn){
 const keys=['REVER_SESSION_TOKEN','REVER_GATEWAY_URL','REVER_MAX_TOOLS','REVER_MAX_TURNS','REVER_ENABLE_REVALIDATION'];
 const saved=Object.fromEntries(keys.map(k=>[k,process.env[k]]));
 Object.assign(process.env,{REVER_SESSION_TOKEN:'scoped-test-token',REVER_GATEWAY_URL:'http://127.0.0.1:8765',REVER_MAX_TOOLS:'10',REVER_MAX_TURNS:'20'});
 delete process.env.REVER_ENABLE_REVALIDATION;
 const profile={method:'mask',recovery,revision:0,accounting:{attempts:0},model:'reasoner',effort:'low',context_window:131072,
  max_output_tokens:4096,request_deadline_ms:10000,prices:{input_per_million:1,output_per_million:2,cached_input_per_million:.2}};
 const original=globalThis.fetch;const recovers=[];
 globalThis.fetch=async(url,options)=>{
  if(url.endsWith('/session'))return new Response(JSON.stringify(profile));
  if(url.endsWith('/recover')){recovers.push(JSON.parse(options.body));return recoverResponse();}
  throw new Error('unexpected gateway path '+url);
 };
 const tools=[];const pi={registerProvider(){},registerTool(t){tools.push(t);},on(){},exec(){throw new Error('no exec');}};
 try{await extension(pi);await fn(tools,recovers);}
 finally{globalThis.fetch=original;for(const k of keys){if(saved[k]===undefined)delete process.env[k];else process.env[k]=saved[k];}}
}

const refusal=kind=>()=>new Response(JSON.stringify({error:{kind}}),{status:422});
const LEGACY={max_chars:999,max_calls:3};
const SPLIT={interface:'split_v1',max_chars:999,max_calls:3};

for(const [label,recovery,name,args] of [
 ['legacy',LEGACY,'recover_evidence',{handle:HANDLE}],
 ['split_v1 read',SPLIT,'recover_evidence',{handle:HANDLE}],
 ['split_v1 search',SPLIT,'search_evidence',{query:'needle'}],
]) for(const kind of ['recovery_quota','archive_not_found'])
 test(`${label} gateway refusal ${kind} is a Pi tool error`,async()=>withExtension(recovery,refusal(kind),async tools=>{
  const {end,result,text}=await runToolThroughPi(tools,name,args);
  assert.equal(end.isError,true);assert.equal(result.isError,true);
  assert.equal(text,`ReVer halted: ${kind}`);
 }));

test('split_v1 invalid arguments are a Pi tool error without HTTP',async()=>withExtension(SPLIT,refusal('unexpected'),async(tools,recovers)=>{
 // Whichever layer rejects first (Pi's schema check or the extension's ABI check),
 // Pi must record a failure and the gateway must not be called.
 for(const args of [{query:'x'.repeat(257)},{handle:HANDLE,query:'mixed'}]){
  const name='query' in args&&!('handle' in args)?'search_evidence':'recover_evidence';
  const {result}=await runToolThroughPi(tools,name,args);
  assert.equal(result.isError,true);
 }
 assert.equal(recovers.length,0);
 // The extension's own check is also a thrown failure, not a success-shaped return.
 await assert.rejects(tools.find(t=>t.name==='recover_evidence').execute('x',{handle:HANDLE,query:'mixed'}),/recovery_arguments/);
 assert.equal(recovers.length,0);
}));

test('cancelled recovery rejects instead of returning a success-shaped result',async()=>withExtension(SPLIT,()=>new Response('{}'),async tools=>{
 const controller=new AbortController();controller.abort();
 const original=globalThis.fetch;
 globalThis.fetch=async(url,options)=>{options.signal?.throwIfAborted();return original(url,options);};
 try{await assert.rejects(tools.find(t=>t.name==='recover_evidence').execute('c',{handle:HANDLE},controller.signal));}
 finally{globalThis.fetch=original;}
}));

test('valid search and exact read stay successful tool results',async()=>withExtension(SPLIT,
 ()=>new Response(JSON.stringify({handle:HANDLE,text:'exact text',start:0})),async tools=>{
 for(const [name,args] of [['search_evidence',{query:'needle'}],['recover_evidence',{handle:HANDLE,start:2}]]){
  const {end,result,text}=await runToolThroughPi(tools,name,args);
  assert.equal(end.isError,false);assert.equal(result.isError,false);assert.match(text,/exact text/);
 }
}));
