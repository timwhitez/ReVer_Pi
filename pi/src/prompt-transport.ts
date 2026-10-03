/** Explicit one-shot print transport; use Pi's public input hook, never patch Pi. */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export function registerPromptTransport(pi: ExtensionAPI, halt: (kind: string) => void) {
  const transport = process.env.REVER_PROMPT_TRANSPORT;
  if (transport === undefined) return;
  let consumed = false;
  pi.on("input", (event, ctx) => {
    const reject = () => {
      halt("invalid_prompt_transport");
      // Pi catches thrown input-hook errors and continues. Handled stops input.
      return {action: "handled" as const};
    };
    if (transport !== "json_v1" || consumed || event.source !== "interactive" ||
        !["json", "print"].includes(ctx.mode) || event.images?.length) return reject();
    consumed = true;
    try {
      const value: unknown = JSON.parse(event.text);
      if (!value || typeof value !== "object" || Array.isArray(value)) return reject();
      const payload = value as {schema?: unknown; prompt?: unknown};
      if (Object.keys(payload).length !== 2 || payload.schema !== 1 ||
          typeof payload.prompt !== "string" || !payload.prompt.trim() ||
          Buffer.byteLength(payload.prompt, "utf8") > 1_000_000) return reject();
      return {action: "transform" as const, text: payload.prompt};
    } catch {
      return reject();
    }
  });
}
