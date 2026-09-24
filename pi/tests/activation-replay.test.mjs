import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as engine from '../node_modules/@earendil-works/pi-coding-agent/dist/core/compaction/compaction.js';
import {replayEntries} from './activation-replay-helper.mjs';
const settings={enabled:true,reserveTokens:66560,keepRecentTokens:20000};
function fixture(toolChars=10){
 const header={type:'session',id:'session'},entries=[header];
 function add(message){const id=`m${entries.length}`;entries.push({type:'message',id,parentId:entries.length===1?null:entries.at(-1).id,timestamp:'2026-09-21T00:00:00Z',message});}
 const usage={input:75000,output:100,cacheRead:0,cacheWrite:0,totalTokens:75100,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
 add({role:'user',content:'Read the tool.',timestamp:0});
 add({role:'assistant',content:[{type:'toolCall',id:'call',name:'read',arguments:{path:'owned.txt'}}],usage,stopReason:'toolUse',timestamp:1});
 add({role:'toolResult',toolCallId:'call',toolName:'read',content:[{type:'text',text:'x'.repeat(toolChars)}],isError:false,timestamp:2});
 add({role:'assistant',content:[{type:'text',text:'observed'}],usage,stopReason:'stop',timestamp:3});
 add({role:'user',content:'Continue with a distinct user turn.',timestamp:4});
 add({role:'assistant',content:[{type:'text',text:'done'}],usage,stopReason:'stop',timestamp:5});
 return entries;
}
test('high reported usage with short visible history has no legal compaction prefix',()=>{
 const r=replayEntries(fixture(),engine,settings,131072);assert.equal(r.threshold_crossings,3);assert.equal(r.preparable_prefixes,0);
});
test('positive control: long visible history supplies a legal cut point',()=>{
 const r=replayEntries(fixture(120000),engine,settings,131072);assert.ok(r.preparable_prefixes>0);assert.ok(r.jointly_eligible_prefixes>0);
});
test('threshold uses strict greater-than, not cumulative bill',()=>{
 assert.equal(engine.shouldCompact(64512,131072,settings),false);assert.equal(engine.shouldCompact(64513,131072,settings),true);
});
test('disabled compaction does not become eligible',()=>{
 const r=replayEntries(fixture(120000),engine,{...settings,enabled:false},131072);assert.equal(r.threshold_crossings,0);
});
test('duplicate entry ID must not inflate prefix counts',()=>{
 const f=fixture();f[2].id=f[1].id;assert.throws(()=>replayEntries(f,engine,settings,131072),/duplicate/);
});
test('forks require explicit branch-aware replay',()=>{
 const f=fixture();f[3].parentId='unavailable-branch';assert.throws(()=>replayEntries(f,engine,settings,131072),/forked/);
});
test('invalid settings rejected',()=>{
 assert.throws(()=>replayEntries(fixture(),engine,{...settings,reserveTokens:131072},131072),/settings/);
});
test('invalid usage rejected rather than interpreted as zero',()=>{
 const f=fixture();f[2].message.usage.input=-1;assert.throws(()=>replayEntries(f,engine,settings,131072),/usage/);
});
test('malformed total usage is not silently treated as a threshold miss',()=>{
 const f=fixture();f[2].message.usage.totalTokens=Infinity;assert.throws(()=>replayEntries(f,engine,settings,131072),/usage/);
});
test('multiple session headers cannot concatenate independent trajectories',()=>{
 const f=fixture();f.splice(2,0,{type:'session',id:'different'});assert.throws(()=>replayEntries(f,engine,settings,131072),/headers/);
});
