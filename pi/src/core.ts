/** Pure transformations: no network, tools, environment secrets or hidden gold. */
import { createHash } from "node:crypto";
import type { Usage } from "@earendil-works/pi-ai";

export type Obj = Record<string, any>;
export const API = "rever-gateway-v1";
export const PROVIDER = "rever-gateway";
const ENVELOPE = "rever-envelope-v1:";

export function stable(value: any): string {
  if (value === undefined || ["function","symbol","bigint"].includes(typeof value) || (typeof value === "number" && !Number.isFinite(value)))
    throw new Error("Non-JSON values cannot be hashed as research state");
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
  return "{" + Object.keys(value).filter(k => value[k] !== undefined).sort()
    .map(k => JSON.stringify(k) + ":" + stable(value[k])).join(",") + "}";
}
export function sha(value: any): string { return createHash("sha256").update(stable(value)).digest("hex"); }

/** Flatten an Error and its .cause chain: undici's "fetch failed" hides the real
 *  transport reason (ECONNRESET, socket hang up, timeouts) in nested causes. */
export function causeChain(error: unknown): string {
  const parts: string[] = []; let depth = 0;
  let current: unknown = error;
  while (current instanceof Error && depth < 5) {
    parts.push(current.message);
    current = (current as Error & { cause?: unknown }).cause;
    depth++;
  }
  if (typeof current === "string" && current) parts.push(current);
  return parts.join(" <- ") || "unknown error";
}
export function textContent(content: any): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) throw new Error("Unsupported Pi content representation");
  if (content.some(x => !x || !["text", "thinking", "toolCall"].includes(x.type)))
    throw new Error("Multimodal/unknown content is not supported; it will not be silently dropped");
  if (content.some(x => x.type === "text" && typeof x.text !== "string"))
    throw new Error("Malformed Pi text block; implicit coercion would lose content");
  return content.filter(x => x.type === "text").map(x => x.text).join("");
}

export function canonicalMessages(context: Obj): Obj[] {
  const output: Obj[] = context.systemPrompt ? [{role:"system", content:context.systemPrompt}] : [];
  for (const message of context.messages) {
    if (["user", "toolResult"].includes(message.role) && Array.isArray(message.content) &&
        message.content.some((b: Obj) => !b || b.type !== "text"))
      throw new Error("User/tool content must be text; non-text blocks cannot be silently discarded");
    if (message.role === "user") {
      output.push({role:"user", content:textContent(message.content)});
    } else if (message.role === "toolResult") {
      output.push({role:"tool", content:textContent(message.content), call_id:message.toolCallId});
    } else if (message.role === "assistant") {
      if (message.api !== API || message.provider !== PROVIDER)
        throw new Error("Cross-provider assistant history is not a supported replay contract");
      const content = textContent(message.content);
      const calls = message.content.filter((x:Obj) => x.type === "toolCall")
        .map((x:Obj) => ({id:x.id, name:x.name, arguments:x.arguments}));
      const envelopes = message.content.filter((x:Obj) => x.type === "thinking" &&
        typeof x.thinkingSignature === "string" && x.thinkingSignature.startsWith(ENVELOPE));
      if (envelopes.length !== 1) throw new Error("Missing/duplicate durable protocol envelope");
      let data: Obj;
      try { data = JSON.parse(Buffer.from(envelopes[0].thinkingSignature.slice(ENVELOPE.length), "base64").toString("utf8")); }
      catch { throw new Error("Corrupt protocol envelope; do not regenerate signed reasoning"); }
      if (data.content_sha !== sha(content) || data.calls_sha !== sha(calls) ||
          data.protocol_sha !== sha({reasoning:data.reasoning ?? null, response_items:data.response_items ?? null}))
        throw new Error("Assistant content was rewritten without its original protocol state");
      output.push({role:"assistant", content, calls,
        reasoning:data.reasoning ?? null, response_items:data.response_items ?? null});
    } else throw new Error("Unknown Pi message role: " + String(message.role));
  }
  return output;
}

export function makeEnvelope(result: Obj): Obj {
  const data = {content_sha:sha(result.text), calls_sha:sha(result.calls),
    reasoning:result.reasoning, response_items:result.response_items,
    protocol_sha:sha({reasoning:result.reasoning ?? null,response_items:result.response_items ?? null})};
  // Unkeyed corruption detection, NOT authenticity or a trust boundary.
  // This is OUR opaque transport envelope, not a fabricated provider signature.
  // Existing reasoning items inside it are stored/replayed exactly, never synthesized.
  return {type:"thinking", thinking:"", redacted:true,
    thinkingSignature:ENVELOPE + Buffer.from(JSON.stringify(data)).toString("base64")};
}

export function usage(raw: Obj | null, prices: Obj): Usage {
  const zero = {input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,
    cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};
  if (!raw) return zero; // UI only. The gateway keeps unknown reservations, NOT zero actual usage.
  const input = raw.input_tokens ?? raw.prompt_tokens;
  const out = raw.output_tokens ?? raw.completion_tokens;
  if (!Number.isSafeInteger(input) || !Number.isSafeInteger(out) || input < 0 || out < 0) return zero;
  for (const [key,expected] of [["input_tokens",input],["prompt_tokens",input],["output_tokens",out],
    ["completion_tokens",out],["total_tokens",input+out]] as [string,number][]) {
    if(key in raw && (!Number.isSafeInteger(raw[key]) || raw[key] !== expected)) return zero;
  }
  const cachedValues: number[] = [], reasoningValues: number[] = [];
  for(const [key,field,ceiling,target] of [
    ["input_tokens_details","cached_tokens",input,cachedValues],
    ["prompt_tokens_details","cached_tokens",input,cachedValues],
    ["output_tokens_details","reasoning_tokens",out,reasoningValues],
    ["completion_tokens_details","reasoning_tokens",out,reasoningValues],
  ] as [string,string,number,number[]][]) {
    const detail=raw[key];
    if(detail == null) continue;
    if(typeof detail !== "object" || Array.isArray(detail)) return zero;
    if(field in detail) {
      const value=detail[field];
      if(!Number.isSafeInteger(value) || value<0 || value>ceiling) return zero;
      target.push(value);
    }
  }
  const cached = cachedValues[0] ?? raw.prompt_cache_hit_tokens ?? 0;
  if (!Number.isSafeInteger(cached) || cached < 0 || cached > input || cachedValues.some(v=>v!==cached)) return zero;
  if(reasoningValues.some(v=>v!==reasoningValues[0])) return zero;
  for(const [key,expected] of [["prompt_cache_hit_tokens",cached],["prompt_cache_miss_tokens",input-cached]] as [string,number][]) {
    if(key in raw && (!Number.isSafeInteger(raw[key]) || raw[key] !== expected)) return zero;
  }
  const cost = {input:(input-cached)*prices.input_per_million/1e6,
    output:out*prices.output_per_million/1e6,cacheRead:cached*prices.cached_input_per_million/1e6,
    cacheWrite:0,total:0};
  cost.total = cost.input + cost.output + cost.cacheRead;
  return {input:input-cached, output:out,cacheRead:cached,cacheWrite:0,totalTokens:input+out,cost};
}

export function recordsFromMessages(messages: Obj[]): Obj[] {
  const records: Obj[] = [];
  let seen: Record<string,number> = {};
  for (const m of messages) {
    let kind: string, text: string, dependencies: Obj = {}, updates: Obj = {};
    if (m.role === "user") { kind = "goal"; text = textContent(m.content); }
    else if (m.role === "assistant") {
      kind = "action";
      const calls = m.content.filter((b:Obj) => b.type === "toolCall").map((b:Obj) => ({id:b.id,name:b.name,arguments:b.arguments}));
      // Readable reasoning may be useful evidence; opaque encrypted content is NOT decoded for summarization.
      let readable = m.content.filter((b:Obj) => b.type === "thinking" && !b.redacted).map((b:Obj) => b.thinking).join("\n");
      const env = m.content.find((b:Obj) => b.type === "thinking" && b.thinkingSignature?.startsWith(ENVELOPE));
      if (env) {
        const data = JSON.parse(Buffer.from(env.thinkingSignature.slice(ENVELOPE.length), "base64").toString("utf8"));
        readable = data.reasoning ?? readable;
      }
      text = stable({assistant:textContent(m.content), calls, readable_reasoning:readable});
    } else if (m.role === "toolResult") {
      const metadata = m.details?.rever_observation;
      kind = metadata?.kind === "verification" ? "verification" : "observation";
      text = stable({tool:m.toolName, call_id:m.toolCallId, is_error:!!m.isError, output:textContent(m.content)});
      dependencies = metadata?.dependencies ?? {};
      updates = metadata?.updates ?? {};
    } else throw new Error("Unconverted AgentMessage in compaction; cancel rather than flatten unknown state");
    if (!text.trim()) continue;
    const fingerprint = sha({role:m.role,text,dependencies,updates,timestamp:m.timestamp ?? null}).slice(0,24);
    const duplicate = seen[fingerprint] ?? 0; seen[fingerprint] = duplicate+1;
    records.push({id:`pi_${fingerprint}_${duplicate}`,kind,text,dependencies,updates,metadata:{origin:"pi-visible-history"}});
  }
  return records;
}

export function safeGateway(url: string): string {
  const u = new URL(url);
  if (!["http:","https:"].includes(u.protocol) || u.username || u.password || u.search || u.hash)
    throw new Error("Invalid gateway URL: credentials/query/fragment forbidden");
  if (u.protocol === "http:" && !["127.0.0.1","localhost","[::1]"].includes(u.hostname) && process.env.REVER_ALLOW_PRIVATE_HTTP !== "1")
    throw new Error("Non-loopback HTTP requires explicit REVER_ALLOW_PRIVATE_HTTP=1 and a restricted network");
  return url.replace(/\/$/, "");
}

/** Bind canonical tool results to error/name metadata without altering any message.
 * Unknown error status is preserved as uncompressible, never guessed successful.
 */
export function observationMetadata(context: Obj): Obj[] {
  return context.messages.filter((m: Obj) => m.role === "toolResult").map((m: Obj) => {
    if (typeof m.toolCallId !== "string" || !m.toolCallId || typeof m.toolName !== "string" || !m.toolName)
      throw new Error("Tool result lacks its observation identity");
    return {call_id:m.toolCallId, tool_name:m.toolName,
      content_sha:sha(textContent(m.content)), is_error:m.isError !== false};
  });
}
