import test from 'node:test';
import assert from 'node:assert/strict';
import {API,PROVIDER,stable,sha,textContent,canonicalMessages,makeEnvelope,usage,recordsFromMessages,safeGateway} from '../src/core.ts';

const result={text:'done 中',calls:[],reasoning:'original reason',response_items:[{type:'reasoning',id:'r',encrypted_content:'opaque'}]};
function assistant(r=result){return {role:'assistant',api:API,provider:PROVIDER,timestamp:1,content:[makeEnvelope(r),{type:'text',text:r.text},...r.calls.map(c=>({type:'toolCall',...c}))]};}
const prices={input_per_million:2,output_per_million:8,cached_input_per_million:1};
test('canonical JSON object ordering and Unicode',()=>{assert.equal(stable({b:1,a:'中'}),'\u007b"a":"中","b":1}');assert.equal(sha({a:1,b:2}),sha({b:2,a:1}));});
for(const bad of [undefined,NaN,Infinity,-Infinity,1n,()=>{},Symbol('x')])test('reject non-JSON state '+String(bad),()=>assert.throws(()=>stable(bad)));
test('text only excludes reasoning/tool blocks',()=>assert.equal(textContent([{type:'thinking',thinking:'private'},{type:'text',text:'answer'},{type:'toolCall',name:'read'}]),'answer'));
test('multimodal not silently discarded',()=>assert.throws(()=>textContent([{type:'image',data:'secret'}])));
test('Chat reasoning and Responses encrypted state preserved',()=>{const a=canonicalMessages({messages:[assistant()]})[0];assert.deepEqual(a.response_items,result.response_items);assert.equal(a.reasoning,result.reasoning);});
test('rewritten text rejects old envelope',()=>{const m=assistant();m.content[1].text='changed';assert.throws(()=>canonicalMessages({messages:[m]}));});
test('rewritten function arguments reject old envelope',()=>{const r={...result,calls:[{id:'call',name:'read',arguments:{path:'a'}}]};const m=assistant(r);m.content.at(-1).arguments={path:'b'};assert.throws(()=>canonicalMessages({messages:[m]}));});
test('foreign provider history is not translated',()=>assert.throws(()=>canonicalMessages({messages:[{...assistant(),provider:'other'}]})));
test('missing durable envelope fails',()=>{const m=assistant();m.content.shift();assert.throws(()=>canonicalMessages({messages:[m]}));});
test('duplicate envelope fails',()=>{const m=assistant();m.content.push(makeEnvelope(result));assert.throws(()=>canonicalMessages({messages:[m]}));});
test('tool-result canonical call ID exact',()=>assert.deepEqual(canonicalMessages({messages:[{role:'toolResult',toolCallId:'c1',content:[{type:'text',text:'x'}]}]}),[{role:'tool',call_id:'c1',content:'x'}]));
test('unknown roles rejected',()=>assert.throws(()=>canonicalMessages({messages:[{role:'hidden'}]})));
test('cached input not double counted',()=>{const u=usage({prompt_tokens:100,completion_tokens:20,prompt_tokens_details:{cached_tokens:70}},prices);assert.equal(u.input,30);assert.equal(u.totalTokens,120);assert.equal(u.cost.total,(30*2+70+20*8)/1e6);});
test('DeepSeek cache field mapped',()=>assert.equal(usage({prompt_tokens:100,completion_tokens:20,prompt_cache_hit_tokens:80},prices).cacheRead,80));
test('malformed UI usage not a fabricated number',()=>assert.equal(usage({input_tokens:-1,output_tokens:20},prices).totalTokens,0));
test('invalid cache subset rejected',()=>assert.equal(usage({input_tokens:1,output_tokens:1,input_tokens_details:{cached_tokens:2}},prices).totalTokens,0));
test('verification versions survive Pi conversion',()=>{const r=recordsFromMessages([{role:'toolResult',toolName:'bash',toolCallId:'x',content:[{type:'text',text:'pass'}],isError:false,details:{rever_observation:{kind:'verification',dependencies:{epoch:'1'},updates:{epoch:'2'}}}}])[0];assert.equal(r.kind,'verification');assert.deepEqual(r.dependencies,{epoch:'1'});assert.match(r.text,/pass/);});
test('tool isError stays observable',()=>assert.match(recordsFromMessages([{role:'toolResult',toolName:'bash',toolCallId:'x',content:[{type:'text',text:'fail'}],isError:true}])[0].text,/"is_error":true/));
test('duplicate occurrence IDs deterministic and unique',()=>{const ms=[{role:'user',content:'hello',timestamp:1},{role:'user',content:'hello',timestamp:1}];const a=recordsFromMessages(ms);assert.notEqual(a[0].id,a[1].id);assert.deepEqual(a,recordsFromMessages(ms));});
test('readable reasoning retained but encrypted blob not decoded',()=>{const r=recordsFromMessages([assistant()])[0];assert.match(r.text,/original reason/);assert.doesNotMatch(r.text,/opaque/);});
for(const bad of ['file:///tmp/a','http://u:p@host','https://x/?key=abc','https://x/#x','http://public.example'])test('gateway rejects '+bad,()=>assert.throws(()=>safeGateway(bad)));
test('loopback and TLS gateway accepted',()=>{assert.equal(safeGateway('http://127.0.0.1:8765/'),'http://127.0.0.1:8765');assert.equal(safeGateway('https://gateway.example'),'https://gateway.example');});

// v1.1 independent edge contracts; no upstream dependency substitution hidden here.
const badCounters = [
 {input_tokens:100,output_tokens:20,total_tokens:121},
 {input_tokens:100,prompt_tokens:101,output_tokens:20},
 {input_tokens:100,output_tokens:20,input_tokens_details:{cached_tokens:10},prompt_cache_hit_tokens:20},
 {input_tokens:100,output_tokens:20,input_tokens_details:{cached_tokens:10},prompt_tokens_details:{cached_tokens:20}},
 {input_tokens:100,output_tokens:20,output_tokens_details:{reasoning_tokens:10},completion_tokens_details:{reasoning_tokens:19}},
 {input_tokens:100,output_tokens:20,input_tokens_details:{},prompt_tokens_details:[]},
 {input_tokens:100,output_tokens:20,output_tokens_details:{},completion_tokens_details:[]},
 {input_tokens:100,output_tokens:20,output_tokens_details:{reasoning_tokens:null}},
 {input_tokens:100,output_tokens:20,output_tokens_details:{reasoning_tokens:21}},
 {input_tokens:Number.MAX_SAFE_INTEGER+1,output_tokens:1},
];
for (const [i,u] of badCounters.entries()) test('v1.1 reject contradictory usage '+i,()=>assert.equal(usage(u,prices).totalTokens,0));
test('v1.1 use known cache detail when other alias is null',()=>assert.equal(usage({input_tokens:100,output_tokens:20,input_tokens_details:null,prompt_tokens_details:{cached_tokens:60}},prices).cacheRead,60));

// v1.2: malformed text must not disappear through Array.join coercion.
for (const bad of [undefined,null,123,{},['text']]) test('v1.2 reject non-string text '+String(bad),()=>{
  assert.throws(()=>textContent([{type:'text',text:bad}]));
});
for (const role of ['user','toolResult']) test('v1.2 reject hidden non-text blocks on '+role,()=>{
  assert.throws(()=>canonicalMessages({messages:[{role,toolCallId:'c',content:[{type:'thinking',thinking:'not user text'},{type:'text',text:'visible'}]}]}));
});

// Early-response-headers contract: errors may ride inside a 200 body as {"error": {...}}.
import { readFileSync } from "node:fs";

for(const field of ['reasoning','response_items']) test('RC4 opaque protocol corruption rejected: '+field,()=>{
 const m=assistant();const block=m.content[0];
 const prefix='rever-envelope-v1:';
 assert.ok(block.thinkingSignature.startsWith(prefix));
 const data=JSON.parse(Buffer.from(block.thinkingSignature.slice(prefix.length),'base64').toString());
 data[field]=field==='reasoning'?'changed':[{type:'reasoning',id:'changed'}];
 block.thinkingSignature=prefix+Buffer.from(JSON.stringify(data)).toString('base64');
 assert.throws(()=>canonicalMessages({messages:[m]}));
});
