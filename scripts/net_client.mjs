/** No model API calls, auth headers or automatic retries. Fresh process per cell. */
import { createHash } from 'node:crypto';
const [url, id, timeoutArg='660000'] = process.argv.slice(2);
const parsed = new URL(url);
if (parsed.protocol !== 'http:' || parsed.username || parsed.password)
  throw new Error('Use only a deliberately configured plain-HTTP diagnostic fixture');
const started=performance.now(); let status=null,phase='await_headers',firstBody=null;
try {
 const response=await fetch(url,{headers:{'X-Diagnostic-ID':id},redirect:'error',
   signal:AbortSignal.timeout(Number(timeoutArg))});
 status=response.status;phase='read_body';const reader=response.body.getReader();const chunks=[];let n=0;
 while (true) {
  const {done,value}=await reader.read();if(done)break;
  if(firstBody===null)firstBody=performance.now()-started;
  n+=value.byteLength;if(n>1048576){await reader.cancel();throw new Error('BodyLimit');}chunks.push(value);
 }
 const raw=Buffer.concat(chunks);const data=JSON.parse(raw.toString('utf8'));
 if(data.id!==id || data.mock_only!==true)throw new Error('ResponseIdentityMismatch');
 console.log(JSON.stringify({id,ok:response.ok,status,phase:'complete',elapsed_ms:performance.now()-started,
   first_body_ms:firstBody,body_sha256:createHash('sha256').update(raw).digest('hex'),node:process.version}));
} catch(error) {
 const chain=[];const seen=new Set();let e=error;
 while(e && !seen.has(e) && chain.length<8){seen.add(e);chain.push({name:e.name,code:e.code??null});e=e.cause;}
 console.log(JSON.stringify({id,ok:false,status,phase,elapsed_ms:performance.now()-started,
   error_chain:chain,node:process.version}));process.exitCode=1;
}
