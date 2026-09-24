/** S4 recursive read-only file allowlist. Not a hostile-code sandbox. */
import { lstatSync, realpathSync } from 'node:fs';
import { resolve, join, isAbsolute } from 'node:path';
import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
export function permittedSourceRead(input: unknown, workspace: string, files: string[]): boolean {
  if (!input || typeof input !== 'object') return false;
  const p=(input as Record<string,unknown>).path;
  if (typeof p !== 'string' || !files.includes(p) || isAbsolute(p) || !/^[A-Za-z0-9_.\-/]+$/.test(p)) return false;
  const parts=p.split('/'); if(parts.some(x=>!x || x==='.' || x==='..')) return false;
  try {
    let path=resolve(workspace); if(realpathSync(path)!==path || lstatSync(path).isSymbolicLink()) return false;
    for(let i=0;i<parts.length;i++) {path=join(path,parts[i]);const st=lstatSync(path);
      if(st.isSymbolicLink() || (i<parts.length-1 ? !st.isDirectory() : !st.isFile())) return false;
    }
    return realpathSync(path)===path;
  } catch { return false; }
}
export default function sourceReadOnly(pi:ExtensionAPI):void {
  const workspace=process.env.REVER_PAIRED_WORKSPACE;
  const files:unknown=JSON.parse(process.env.REVER_PAIRED_FILES??'null');
  if(!workspace || !Array.isArray(files) || files.some(x=>typeof x!=='string')) throw new Error('Missing S4 public snapshot');
  pi.on('tool_call',async(event,ctx)=>{
    if(['recover_evidence','search_evidence'].includes(event.toolName)) return;
    if(event.toolName==='read' && permittedSourceRead(event.input,workspace,files)) return;
    ctx.abort();return {block:true,reason:'S4 read-only source path/tool not permitted'};
  });
}
