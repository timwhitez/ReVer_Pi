#!/usr/bin/env node
/** Read-only inspection of the exact pinned Pi cut preparation. No actor/network. */
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pkg = path.join(root, 'pi/node_modules/@earendil-works/pi-coding-agent');
const source = path.join(pkg, 'dist/core/compaction/compaction.js');
const expected = 'fcb12f1eb4d38578978e1a8e3e382a3fccfd5e0ccf87bc86979a9a8d9c145c7b';
if (process.argv.length !== 4) throw new Error('Usage: inspect_compaction_boundary.mjs SESSION_JSONL SETTINGS_JSON');
if (JSON.parse(fs.readFileSync(path.join(pkg,'package.json'))).version !== '0.84.2') throw new Error('Pinned Pi required');
if (createHash('sha256').update(fs.readFileSync(source)).digest('hex') !== expected) throw new Error('Pinned implementation differs');
const entries = fs.readFileSync(process.argv[2],'utf8').split('\n').filter(x=>x.trim()).map(JSON.parse);
const settings = JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
const engine = await import(pathToFileURL(source));
const {replayEntries} = await import('../pi/tests/activation-replay-helper.mjs');
const replay = replayEntries(entries,engine,settings.compaction,131072);
const preparation = engine.prepareCompaction(entries.slice(1),settings.compaction);
console.log(JSON.stringify({schema:1,pi_version:'0.84.2',pi_compaction_js_sha256:expected,
    legal_preparation:preparation!==undefined,first_kept_id:preparation?.firstKeptEntryId??null,
    archived_messages:preparation?preparation.messagesToSummarize.length+preparation.turnPrefixMessages.length:0,
    usage_gate_crossed:replay.checks.at(-1)?.threshold_crossed??false,
    visible_char_estimate:replay.checks.at(-1)?.visible_estimate_tokens??0,
    settings,read_only:true,scripted_usage_not_model_measurement:true}));
