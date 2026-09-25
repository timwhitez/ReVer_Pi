/** Test helper: run one tool call through the REAL pinned Pi agent loop with a
 * scripted model stream. Not a model, not a Provider, not evidence of quality. */
import assert from 'node:assert/strict';
import {agentLoop} from '@earendil-works/pi-agent-core';
import {createAssistantMessageEventStream} from '@earendil-works/pi-ai';

const MODEL={api:'scripted',provider:'scripted',id:'scripted'};

/** Drive one tool call through the real agentLoop and return what Pi recorded. */
export async function runToolThroughPi(tools,name,args,cwd){
 let turn=0;
 const streamFn=()=>{
  const stream=createAssistantMessageEventStream();
  const base={role:'assistant',api:MODEL.api,provider:MODEL.provider,model:MODEL.id,
   usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}},timestamp:Date.now()};
  const message=turn++===0
   ?{...base,content:[{type:'toolCall',id:'call-1',name,arguments:args}],stopReason:'toolUse'}
   :{...base,content:[{type:'text',text:'done'}],stopReason:'stop'};
  queueMicrotask(()=>{stream.push({type:'start',partial:message});stream.push({type:'done',reason:message.stopReason,message});stream.end();});
  return stream;
 };
 // Pi's coding agent passes the extension context as execute's fifth argument.
 if(cwd!==undefined)tools=tools.map(t=>({...t,execute:(id,a,signal,update)=>t.execute(id,a,signal,update,{cwd})}));
 const events=[];
 const stream=agentLoop([{role:'user',content:'go',timestamp:Date.now()}],{systemPrompt:'',messages:[],tools},
  {model:MODEL,convertToLlm:m=>m,toolExecution:'sequential'},undefined,streamFn);
 for await(const event of stream)events.push(event);
 const end=events.find(e=>e.type==='tool_execution_end');
 const result=events.find(e=>e.type==='message_end'&&e.message.role==='toolResult')?.message;
 assert.ok(end&&result,'tool call did not reach Pi execution');
 return {end,result,text:result.content.map(c=>c.text).join('')};
}
