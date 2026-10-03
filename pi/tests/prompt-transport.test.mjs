import assert from "node:assert/strict";
import test from "node:test";
import { registerPromptTransport } from "../src/prompt-transport.ts";

function registered(transport = "json_v1") {
  const before = process.env.REVER_PROMPT_TRANSPORT;
  if (transport === undefined) delete process.env.REVER_PROMPT_TRANSPORT;
  else process.env.REVER_PROMPT_TRANSPORT = transport;
  let input, fatal;
  const errors = [];
  try { registerPromptTransport({on(name, fn) { assert.equal(name, "input"); input = fn; }}, kind => { fatal = kind; errors.push(kind); }); }
  finally {
    if (before === undefined) delete process.env.REVER_PROMPT_TRANSPORT;
    else process.env.REVER_PROMPT_TRANSPORT = before;
  }
  return {get fatal() { return fatal; }, call(text, options = {}) {
    const result = input({text, source: options.source ?? "interactive", images: options.images}, {mode: options.mode ?? "json"});
    return {result, errors};
  }, input};
}

test("raw prompt is restored only inside an opted-in one-shot print input", () => {
  const task = "\t\r\n中文😀\u2028 /literal @file --print \n  ";
  for (const mode of ["json", "print"]) {
    const r = registered();
    assert.deepEqual(r.call(JSON.stringify({schema: 1, prompt: task}), {mode}).result, {action: "transform", text: task});
    assert.equal(r.call(JSON.stringify({schema: 1, prompt: "second"}), {mode}).result.action, "handled");
    assert.equal(r.fatal, "invalid_prompt_transport");
  }
});

test("ordinary RPC/CLI remains unregistered without the opt-in", () => {
  // Passing explicit undefined uses the default; register directly for absent env.
  const before = process.env.REVER_PROMPT_TRANSPORT;
  delete process.env.REVER_PROMPT_TRANSPORT;
  try { registerPromptTransport({on() { assert.fail("must not intercept ordinary input"); }}, () => assert.fail()); }
  finally { if (before !== undefined) process.env.REVER_PROMPT_TRANSPORT = before; }
});

test("UTF-8 byte limit accepts exactly one million bytes", () => {
  const prompt = "😀".repeat(250000);
  assert.equal(Buffer.byteLength(prompt), 1000000);
  assert.equal(registered().call(JSON.stringify({schema: 1, prompt})).result.text, prompt);
  assert.equal(registered().call(JSON.stringify({schema: 1, prompt: prompt + "x"})).result.action, "handled");
});

test("malformed, wrong-schema/source/mode and repeated transport fail closed without prompt logging", () => {
  const secret = "SYNTHETIC_PRIVATE_TASK";
  const invalid = [secret, "null", "[]", JSON.stringify({schema: 2, prompt: secret}),
    JSON.stringify({schema: 1, prompt: 3}), JSON.stringify({schema: 1, prompt: " \n"}),
    JSON.stringify({schema: 1, prompt: secret, extra: true}), JSON.stringify({prompt: secret})];
  for (const text of invalid) {
    const r = registered(), outcome = r.call(text);
    assert.equal(outcome.result.action, "handled");
    assert.equal(r.fatal, "invalid_prompt_transport");
    assert.deepEqual(outcome.errors, ["invalid_prompt_transport"]);
  }
  const valid = JSON.stringify({schema: 1, prompt: secret});
  for (const options of [{source: "rpc"}, {source: "extension"}, {mode: "rpc"}, {mode: "tui"}, {images: [{}]}])
    assert.equal(registered().call(valid, options).result.action, "handled");
  assert.equal(registered("unknown").call(valid).result.action, "handled");
});
