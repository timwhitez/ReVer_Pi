import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,writeFileSync,readFileSync,rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import register from '../src/revalidation-extension.ts';
const exec=promisify(execFile);
const hash=p=>createHash('sha256').update(readFileSync(p)).digest('hex');
const keys=['REVER_ENABLE_REVALIDATION','REVER_VERIFIER_REGISTRY','REVER_VERIFIER_REGISTRY_SHA256',
'REVER_VERIFIER_RUNNER','REVER_VERIFIER_RUNNER_SHA256','REVER_VERIFIER_PYTHON','REVER_MAX_REVALIDATIONS'];
async function fixture(fn) {
 const old=Object.fromEntries(keys.map(k=>[k,process.env[k]]));const root=mkdtempSync(join(tmpdir(),'rever-verify-'));
 const runner=resolve('../src/reverpi/revalidation.py');
 writeFileSync(join(root,'check.py'),'assert 1 == 1\n');
 writeFileSync(join(root,'registry.json'),JSON.stringify({schema:1,verifiers:[{id:'check',
 argv:['python3','-B','check.py'],inputs:['check.py'],timeout_seconds:2}]}));
 Object.assign(process.env,{REVER_ENABLE_REVALIDATION:'1',REVER_VERIFIER_REGISTRY:join(root,'registry.json'),
 REVER_VERIFIER_REGISTRY_SHA256:hash(join(root,'registry.json')),REVER_VERIFIER_RUNNER:runner,
 REVER_VERIFIER_RUNNER_SHA256:hash(runner),REVER_VERIFIER_PYTHON:'python3',REVER_MAX_REVALIDATIONS:'3'});
 const tools=[];const pi={registerTool:t=>tools.push(t),exec:async(command,args,opt)=>{
  try{const r=await exec(command,args,{signal:opt.signal,timeout:opt.timeout,maxBuffer:2*1024*1024});return {...r,code:0,killed:false};}
  catch(e){return {stdout:e.stdout??'',stderr:e.stderr??'',code:e.code??2,killed:!!e.killed};}
 }};
 try{await fn({root,tools,pi});}finally{for(const k of keys)if(old[k]===undefined)delete process.env[k];else process.env[k]=old[k];rmSync(root,{recursive:true,force:true});}
}

test('revalidation disabled by default',async()=>fixture(async({pi,tools})=>{
 delete process.env.REVER_ENABLE_REVALIDATION;register(pi);assert.equal(tools.length,0);
}));
test('registry identity required before registration',async()=>fixture(async({pi})=>{
 process.env.REVER_VERIFIER_REGISTRY_SHA256='0'.repeat(64);assert.throws(()=>register(pi));
}));
test('callback invokes a REAL verifier, observes current fail after old pass',async()=>fixture(async({pi,tools,root})=>{
 register(pi);const tool=tools[0];
 const first=await tool.execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root});
 assert.equal(JSON.parse(first.content[0].text).passed,true);
 writeFileSync(join(root,'check.py'),'assert 1 == 2\n');
 const second=await tool.execute('2',{verifier_id:'check'},undefined,undefined,{cwd:root});
 const receipt=JSON.parse(second.content[0].text);assert.equal(receipt.passed,false);assert.equal(receipt.status,'completed');
 assert.equal(receipt.dependency_completeness_certified,false);
}));
test('tool denies arbitrary command as verifier id',async()=>fixture(async({pi,tools,root})=>{
 register(pi);const r=await tools[0].execute('1',{verifier_id:'echo arbitrary-command'},undefined,undefined,{cwd:root});assert.equal(r.isError,true);
}));
test('changed runner fails identity before execution',async()=>fixture(async({pi,tools,root})=>{
 register(pi);writeFileSync(join(root,'registry.json'),'{}');
 const r=await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root});assert.equal(r.isError,true);
}));
test('quota is explicit',async()=>fixture(async({pi,tools,root})=>{
 process.env.REVER_MAX_REVALIDATIONS='1';register(pi);await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root});
 const r=await tools[0].execute('2',{verifier_id:'check'},undefined,undefined,{cwd:root});assert.equal(r.isError,true);
}));

// RC4: these use a real Python runner, except the explicitly replayed response.
test('a cached receipt from the previous invocation is rejected',async()=>fixture(async({pi,tools,root})=>{
 let captured;const original=pi.exec;
 pi.exec=async(...args)=>{captured=await original(...args);return captured;};
 register(pi);assert.equal((await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root})).isError,undefined);
 pi.exec=async()=>captured;
 assert.equal((await tools[0].execute('2',{verifier_id:'check'},undefined,undefined,{cwd:root})).isError,true);
}));
test('changing interpreter environment after registration cannot substitute runtime',async()=>fixture(async({pi,tools,root})=>{
 register(pi);process.env.REVER_VERIFIER_PYTHON='/definitely/not/a/python';
 const r=await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root});
 assert.equal(r.isError,undefined);const receipt=JSON.parse(r.content[0].text);
 assert.equal(receipt.invocation_nonce.length,64);assert.equal(receipt.receipt_is_cryptographic_attestation,false);
}));
test('receipt from another workspace fails even with a current nonce',async()=>fixture(async({pi,tools,root})=>{
 const original=pi.exec;pi.exec=async(...args)=>{const r=await original(...args);const receipt=JSON.parse(r.stdout);
 receipt.workspace_path_sha256='0'.repeat(64);return {...r,stdout:JSON.stringify(receipt)};};
 register(pi);assert.equal((await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root})).isError,true);
}));
test('passing receipt cannot claim a nonzero process exit',async()=>fixture(async({pi,tools,root})=>{
 const original=pi.exec;pi.exec=async(...args)=>{const r=await original(...args);const receipt=JSON.parse(r.stdout);
 receipt.returncode=2;return {...r,stdout:JSON.stringify(receipt)};};
 register(pi);assert.equal((await tools[0].execute('1',{verifier_id:'check'},undefined,undefined,{cwd:root})).isError,true);
}));
