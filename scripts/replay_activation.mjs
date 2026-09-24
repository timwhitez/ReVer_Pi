#!/usr/bin/env node
/** Offline counterfactual replay using the pinned Pi installed by the operator. */
import path from 'node:path';
import {run} from '../pi/tests/activation-replay-helper.mjs';
const args=process.argv.slice(2);const flags={};
for(let i=0;i<args.length;i+=2){
 if(!['--supplement','--audit','--out'].includes(args[i])||!args[i+1]||flags[args[i]])throw Error('Expected --supplement DIR --audit JSON --out NEW_JSON');
 flags[args[i]]=path.resolve(args[i+1]);
}
if(Object.keys(flags).length!==3)throw Error('Expected --supplement DIR --audit JSON --out NEW_JSON');
const r=await run(flags['--supplement'],flags['--audit'],flags['--out']);
console.log(JSON.stringify({available_sessions:r.available_sessions,missing_sessions:r.missing_sessions,
 assistant_prefixes:r.assistant_prefixes,threshold_crossings:r.threshold_crossings,
 preparable_prefixes:r.preparable_prefixes,jointly_eligible_prefixes:r.jointly_eligible_prefixes,new_model_calls:0}));
