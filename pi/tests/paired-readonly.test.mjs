import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, symlinkSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { permittedRead } from '../src/paired-readonly.ts';

test('RC7 exact owned note path only', () => {
  const p=mkdtempSync(join(tmpdir(),'rc7-note-'));
  try {
    writeFileSync(join(p,'note.txt'),'data');
    assert.equal(permittedRead({path:'note.txt'},p,['note.txt']),true);
    for(const input of [null,{}, {path:42},{path:'../note.txt'},{path:'/etc/passwd'},{path:'note.txt/../note.txt'},{path:'other.txt'}])
      assert.equal(permittedRead(input,p,['note.txt']),false);
  } finally {rmSync(p,{recursive:true,force:true});}
});
test('RC7 symlink note rejected',()=>{
  const p=mkdtempSync(join(tmpdir(),'rc7-link-'));
  try{writeFileSync(join(p,'real.txt'),'data');symlinkSync(join(p,'real.txt'),join(p,'note.txt'));
      assert.equal(permittedRead({path:'note.txt'},p,['note.txt']),false);
  }finally{rmSync(p,{recursive:true,force:true});}
});
