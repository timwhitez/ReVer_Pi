import test from 'node:test';
import assert from 'node:assert/strict';
import {observationMetadata,sha,canonicalMessages} from '../src/core.ts';
for (const [value,expected] of [[false,false],[true,true],[undefined,true],[null,true]]) {
  test(`online tool error metadata ${String(value)} is conservative`,()=>{
    const m={role:'toolResult',toolCallId:'c',toolName:'read',isError:value,content:[{type:'text',text:'中😀'}]};
    const before=structuredClone(m);const [meta]=observationMetadata({messages:[m]});
    assert.equal(meta.is_error,expected);assert.equal(meta.content_sha,sha('中😀'));assert.deepEqual(m,before);
  });
}
test('online metadata does not alter canonical history or expose secrets',()=>{
  const context={messages:[{role:'user',content:'goal'},{role:'toolResult',toolCallId:'c',toolName:'read',isError:false,content:[{type:'text',text:'a'}]}]};
  const before=canonicalMessages(context);assert.equal(observationMetadata(context).length,1);assert.deepEqual(canonicalMessages(context),before);
});
for (const changes of [{toolName:''},{toolName:undefined},{toolCallId:''}]) test('online requires tool identity '+JSON.stringify(changes),()=>{
  assert.throws(()=>observationMetadata({messages:[{role:'toolResult',toolCallId:'c',toolName:'read',content:[],...changes}]}));
});
