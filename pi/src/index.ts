/** ReVer-Pi extension. User installation must pass the REAL Pi typecheck/probe. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { convertToLlm } from "@earendil-works/pi-coding-agent";
import { createAssistantMessageEventStream, type AssistantMessage } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { randomUUID } from "node:crypto";
import registerRevalidation from "./revalidation-extension.ts";
import { API, PROVIDER, canonicalMessages, causeChain, makeEnvelope, observationMetadata, recordsFromMessages, safeGateway, sha, usage, type Obj } from "./core.ts";

export default async function extension(pi: ExtensionAPI) {
  const endpoint = safeGateway(process.env.REVER_GATEWAY_URL ?? "http://127.0.0.1:8765");
  const token = process.env.REVER_SESSION_TOKEN;
  if (!token) throw new Error("REVER_SESSION_TOKEN is required (not the upstream Provider key)");
  let fatal: string | null = null;
  let revision = 0, workspaceEpoch = 0, toolCount = 0, turns = 0;
  let session: Obj;
  const dispatchEpoch = new Map<string, number>();
  const maxTools = Number(process.env.REVER_MAX_TOOLS ?? 200);
  const maxTurns = Number(process.env.REVER_MAX_TURNS ?? 80);
  if (![maxTools,maxTurns].every(n => Number.isInteger(n) && n > 0)) throw new Error("Invalid task limits");

  async function request(path: string, body?: Obj, signal?: AbortSignal) {
    const timeout = AbortSignal.timeout(Number(session?.request_deadline_ms ?? 660000));
    const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
    const response = await fetch(endpoint + path, {
      method:body ? "POST" : "GET", headers:{Authorization:`Bearer ${token}`,"Content-Type":"application/json"},
      body:body ? JSON.stringify(body) : undefined, signal:combined, redirect:"error"
    });
    // No client-side retry. An interrupted response may already have been billed.
    const reader = response.body?.getReader();
    if (!reader) throw new Error("Empty gateway response body");
    const buffers: Uint8Array[] = []; let length = 0;
    while (true) { const {done,value} = await reader.read(); if(done) break;
      length += value.byteLength; if(length > 32*1024*1024) { await reader.cancel(); throw new Error("Gateway response size limit"); }
      buffers.push(value);
    }
    const data = JSON.parse(Buffer.concat(buffers).toString("utf8"));
    // Errors arrive via HTTP status OR inside a 200 body when the gateway sends
    // early response headers (restricted egress paths need a non-silent flow).
    const err = !response.ok ? data?.error : (data && typeof data === "object" && "error" in data ? data.error : undefined);
    if (!response.ok || (data && typeof data === "object" && "error" in data)) {
      const kind = err?.kind ?? "gateway_error";
      // Only genuine context overflow may activate Pi's compaction retry path.
      throw new Error(kind === "context_overflow" ? "context_length_exceeded" : `ReVer halted: ${kind}`);
    }
    return data;
  }
  session = await request("/session");
  if (!session.recovery || !Number.isSafeInteger(session.recovery.max_chars) || session.recovery.max_chars < 1 ||
      !Number.isSafeInteger(session.recovery.max_calls) || session.recovery.max_calls < 0)
    throw new Error("Missing/invalid gateway recovery capabilities; require a matching framework version");
  const recoveryInterface = session.recovery.interface ?? "legacy";
  if (!["legacy", "split_v1"].includes(recoveryInterface)) throw new Error("Unknown recovery interface");
  revision = session.revision;
  if (revision !== 0 || session.accounting.attempts !== 0) throw new Error("Fresh task requires a new gateway session; automatic native-task replay is disabled");
  registerRevalidation(pi); // Explicit operator env enables it; no model-selected commands.
  pi.registerProvider(PROVIDER, {
    name:"ReVer-Pi research gateway", baseUrl:endpoint, apiKey:"$REVER_SESSION_TOKEN", api:API,
    models:[{id:session.model,name:session.model,reasoning:true,input:["text"],
      contextWindow:session.context_window,maxTokens:session.max_output_tokens,
      cost:{input:session.prices.input_per_million,output:session.prices.output_per_million,
        cacheRead:session.prices.cached_input_per_million,cacheWrite:0},
      thinkingLevelMap:{minimal:null,low:session.effort === "low" ? "low" : null,
        medium:session.effort === "medium" ? "medium" : null, high:session.effort === "high" ? "high" : null,
        xhigh:session.effort === "xhigh" ? "xhigh" : null,max:session.effort === "max" ? "max" : null}}],
    streamSimple(model, context, options) {
      const stream = createAssistantMessageEventStream();
      const message: AssistantMessage = {role:"assistant",content:[],api:model.api,provider:model.provider,model:model.id,
        usage:usage(null,session.prices),stopReason:"pending",timestamp:Date.now()};
      void (async () => {
        try {
          if (fatal) throw new Error(fatal);
          if (options?.reasoning && options.reasoning !== session.effort) throw new Error("Pi changed the frozen reasoning effort");
          stream.push({type:"start",partial:message});
          const tools = (context.tools ?? []).map(t => ({name:t.name,description:t.description,parameters:t.parameters}));
          const response = await request("/complete", {op:randomUUID(),messages:canonicalMessages(context),tools,
            ...(session.online_projection?.mode && session.online_projection.mode !== "off"
              ? {observation_meta:observationMetadata(context)} : {}),
            effort:session.effort,max_output_tokens:Math.min(options?.maxTokens ?? session.max_output_tokens,session.max_output_tokens)},options?.signal);
          message.content.push(makeEnvelope(response) as any);
          if(response.text) {
            const i = message.content.length;
            message.content.push({type:"text",text:response.text});
            stream.push({type:"text_start",contentIndex:i,partial:message});
            stream.push({type:"text_delta",contentIndex:i,delta:response.text,partial:message});
            stream.push({type:"text_end",contentIndex:i,content:response.text,partial:message});
          }
          for (const call of response.calls) {
            const i = message.content.length;
            const block = {type:"toolCall" as const,id:call.id,name:call.name,arguments:call.arguments};
            message.content.push(block);
            stream.push({type:"toolcall_start",contentIndex:i,partial:message});
            stream.push({type:"toolcall_delta",contentIndex:i,delta:JSON.stringify(call.arguments),partial:message});
            stream.push({type:"toolcall_end",contentIndex:i,toolCall:block,partial:message});
          }
          message.responseId = response.response_id ?? undefined;
          message.responseModel = response.model;
          message.usage = usage(response.usage,session.prices);
          message.stopReason = response.calls.length ? "toolUse" : "stop";
          stream.push({type:"done",reason:message.stopReason,message});
        } catch(error) {
          message.stopReason = options?.signal?.aborted ? "aborted" : "error";
          message.errorMessage = error instanceof Error ? causeChain(error) : "ReVer gateway failure";
          stream.push({type:"error",reason:message.stopReason,error:message});
        } finally { stream.end(); }
      })();
      return stream;
    }
  });

  if (session.method !== "pi_original" && session.recovery.max_calls > 0) {
    if (recoveryInterface === "legacy") {
      // Frozen legacy ABI. Do not silently reinterpret mixed handle/query calls.
      pi.registerTool({
        name:"recover_evidence",label:"Recover archived evidence",
        description:"Retrieve exact historical evidence by handle OR literal search. Retrieval does not revalidate the current workspace.",
        parameters:Type.Object({handle:Type.Optional(Type.String()),query:Type.Optional(Type.String()),
          start:Type.Optional(Type.Integer({minimum:0})),chars:Type.Optional(Type.Integer({minimum:1,maximum:session.recovery.max_chars}))},{additionalProperties:false}),
        async execute(toolCallId, params, signal) {
          try { const result = await request("/recover",{op:sha({toolCallId,params}),...params},signal);
            return {content:[{type:"text",text:JSON.stringify(result)}],details:{archived:true}};
          } catch(error) { return {content:[{type:"text",text:error instanceof Error ? error.message : "Recovery failed"}],details:undefined,isError:true}; }
        }
      });
    } else {
      const executeRecovery = async (toolName: string, toolCallId: string, params: Obj, signal?: AbortSignal) => {
        try {
          // Defense in depth: Pi's schema validation is not assumed to reject
          // unsupported properties for every provider/framework version.
          const search = toolName === "search_evidence";
          const allowed = search ? ["query", "chars"] : ["handle", "start", "chars"];
          const valid = Object.keys(params).every(k => allowed.includes(k)) &&
            (search ? typeof params.query === "string" && params.query.length >= 1 && Array.from(params.query).length <= 256
              : typeof params.handle === "string" && /^[0-9a-f]{64}$/.test(params.handle)) &&
            (params.start === undefined || Number.isSafeInteger(params.start) && params.start >= 0) &&
            (params.chars === undefined || Number.isSafeInteger(params.chars) && params.chars >= 1 && params.chars <= session.recovery.max_chars);
          if (!valid) throw new Error("ReVer halted: recovery_arguments; use search_evidence(query, chars) OR recover_evidence(handle, start, chars)");
          const result = await request("/recover",{op:sha({toolName,toolCallId,params}),chars:Math.min(2000,session.recovery.max_chars),...params},signal);
          return {content:[{type:"text" as const,text:JSON.stringify(result)}],details:{archived:true}};
        } catch(error) {
          return {content:[{type:"text" as const,text:error instanceof Error ? error.message : "Recovery failed"}],details:undefined,isError:true};
        }
      };
      pi.registerTool({
        name:"recover_evidence",label:"Read archived evidence",
        description:"Read an exact interval of historical text by handle. start and chars count Unicode codepoints; start defaults to 0; chars defaults to the smaller of 2000 and the configured cap. For unknown location use search_evidence. Does not revalidate current state.",
        parameters:Type.Object({handle:Type.String({pattern:"^[0-9a-f]{64}$"}),
          start:Type.Optional(Type.Integer({minimum:0})),chars:Type.Optional(Type.Integer({minimum:1,maximum:session.recovery.max_chars}))},{additionalProperties:false}),
        execute:(toolCallId,params,signal)=>executeRecovery("recover_evidence",toolCallId,params,signal)
      });
      pi.registerTool({
        name:"search_evidence",label:"Search archived evidence",
        description:"Locate a literal substring in historical archived text. Returns handles and bounded excerpts; query matching is ASCII case-insensitive. Read a larger exact interval with recover_evidence. Does not revalidate current state.",
        parameters:Type.Object({query:Type.String({minLength:1,maxLength:256}),
          chars:Type.Optional(Type.Integer({minimum:1,maximum:session.recovery.max_chars}))},{additionalProperties:false}),
        execute:(toolCallId,params,signal)=>executeRecovery("search_evidence",toolCallId,params,signal)
      });
    }
  }

  // Conservative, coarse workspace epochs. Unknown shell commands MAY mutate state.
  // This is NOT an inferred precise file-dependency graph or proof of test validity.
  pi.on("tool_call", async (event,ctx) => {
    if(fatal || ++toolCount > maxTools) { fatal = fatal ?? "Task tool limit reached"; ctx.abort(); return {block:true,reason:fatal}; }
    if(!["read","grep","find","ls","recover_evidence","search_evidence"].includes(event.toolName)) workspaceEpoch++;
    dispatchEpoch.set(event.toolCallId, workspaceEpoch);
  });
  pi.on("tool_result", async event => {
    const startedEpoch = dispatchEpoch.get(event.toolCallId);
    dispatchEpoch.delete(event.toolCallId);
    if(["recover_evidence","search_evidence"].includes(event.toolName) || session.method === "pi_original") return;
    const suspectedTest = event.toolName === "bash" && /\b(pytest|unittest|test|ctest)\b/.test(String((event.input as Obj)?.command ?? ""));
    return {details:{...(event.details && typeof event.details === "object" ? event.details as Obj : {}),
      rever_observation:{kind:suspectedTest ? "verification" : "observation",
        dependencies:{workspace_epoch:startedEpoch === undefined ? "UNKNOWN" : String(startedEpoch)},updates:{workspace_epoch:String(workspaceEpoch)},
        certainty:"coarse_observed_epoch_not_complete_environment_identity"}}};
  });
  pi.on("turn_end", async (_event,ctx) => { if(++turns >= maxTurns) { fatal="Task turn limit reached"; ctx.abort(); } });
  // A cell cannot be reused for another branch/session without a new isolated snapshot.
  pi.on("session_before_fork", async () => ({cancel:true}));
  pi.on("session_before_tree", async () => ({cancel:true}));
  pi.on("session_before_switch", async () => ({cancel:true}));

  pi.on("session_before_compact", async (event,ctx) => {
    if(session.online_projection?.mode && session.online_projection.mode !== "off") return; // Native fallback unchanged across observe/apply.
    if(["pi_native","pi_original"].includes(session.method)) return; // Actual Pi native compactor, still same Provider gateway.
    if(fatal) return {cancel:true};
    try {
      const p = event.preparation;
      const source = convertToLlm([...p.messagesToSummarize, ...p.turnPrefixMessages]);
      const records = recordsFromMessages(source);
      const result = await request("/compact", {op:sha({revision,records,boundary:p.firstKeptEntryId}),
        expected_revision:revision,records,active_query:""},event.signal);
      if(event.signal.aborted) throw new Error("Compaction cancelled after response; reconcile session before resuming");
      revision = result.revision;
      return {compaction:{summary:result.memory.text,firstKeptEntryId:p.firstKeptEntryId,tokensBefore:p.tokensBefore,
        details:{rever:{revision,input_sha:result.memory.input_sha,method:session.method}}}};
    } catch(error) {
      fatal = error instanceof Error ? error.message : "Compaction rejected";
      if(ctx.hasUI) ctx.ui.notify("ReVer kept the old memory and stopped: " + fatal,"error");
      ctx.abort();
      return {cancel:true}; // NEVER silently fall through to a different algorithm.
    }
  });
}
