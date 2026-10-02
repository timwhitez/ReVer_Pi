/** Driven by pytest's real loopback gateway; Pi's model stream is scripted. */
import assert from 'node:assert/strict';
import {runToolThroughPi} from './pi-loop-helper.mjs';
import extension from '../src/index.ts';

let input='';for await(const chunk of process.stdin)input+=chunk;
const config=JSON.parse(input);
Object.assign(process.env,{REVER_GATEWAY_URL:config.url,REVER_SESSION_TOKEN:config.token,
 REVER_MAX_TOOLS:'20',REVER_MAX_TURNS:'20'});
delete process.env.REVER_ENABLE_REVALIDATION;
const realFetch=globalThis.fetch;const bodies=[];
globalThis.fetch=async(url,options)=>{
 assert.ok(url.startsWith(config.url+'/'),'unexpected HTTP destination');
 if(url.endsWith('/recover'))bodies.push(JSON.parse(options.body));
 return realFetch(url,options);
};
const tools=[];
await extension({registerProvider(){},registerTool(t){tools.push(t);},on(){},exec(){throw new Error('no exec');}});
const missing=await runToolThroughPi(tools,'recover_evidence',{handle:'0'.repeat(64)});
assert.equal(missing.result.isError,true);assert.equal(missing.end.isError,true);
assert.equal(missing.text,'ReVer halted: archive_not_found');
assert.equal(bodies.length,1);bodies.length=0;
const read=await runToolThroughPi(tools,'recover_evidence',{handle:config.handle});
assert.equal(read.result.isError,false);assert.equal(read.end.isError,false);
assert.equal(JSON.parse(read.text).text,config.text);
const searchName=config.interface==='legacy'?'recover_evidence':'search_evidence';
const search=await runToolThroughPi(tools,searchName,{query:'needle'});
assert.equal(search.result.isError,false);assert.equal(search.end.isError,false);
assert.equal(JSON.parse(search.text).matches[0].excerpt,config.text);
const replay=await runToolThroughPi(tools,'recover_evidence',{handle:config.handle});
assert.equal(replay.result.isError,false);assert.deepEqual(JSON.parse(replay.text),JSON.parse(read.text));
const refused=await runToolThroughPi(tools,'recover_evidence',{handle:config.handle,start:1});
assert.equal(refused.result.isError,true);assert.equal(refused.end.isError,true);
assert.equal(refused.text,'ReVer halted: recovery_quota');
assert.equal(bodies[0].op,bodies[2].op);
for(const body of bodies.slice(0,3)){
 if(config.interface==='legacy')assert.equal(Object.hasOwn(body,'chars'),false);
 else assert.equal(body.chars,100);
}
const badArgs=await runToolThroughPi(tools,'recover_evidence',{handle:config.handle,chars:101});
assert.equal(badArgs.result.isError,true);
process.stdout.write(JSON.stringify({successful:2,refused:3,replayed:true}));
