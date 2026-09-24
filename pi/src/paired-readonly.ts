/** RC7 owned read-only diagnostic, not a general-purpose security sandbox.
 * Loaded ONLY by paired_prefix_canary. Does not affect the product extension.
 */
import { realpathSync, lstatSync } from "node:fs";
import { resolve } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
export function permittedRead(input: unknown, workspace: string, files: string[]): boolean {
  if (!input || typeof input !== "object") return false;
  const p = (input as Record<string, unknown>).path;
  if (typeof p !== "string" || !files.includes(p) || p.includes("/") || p.includes("\\")) return false;
  try {
    const path = resolve(workspace, p);
    return lstatSync(path).isFile() && !lstatSync(path).isSymbolicLink() && realpathSync(path) === path;
  } catch { return false; }
}
export default function pairedReadOnly(pi: ExtensionAPI): void {
  const workspace = process.env.REVER_PAIRED_WORKSPACE;
  const files = JSON.parse(process.env.REVER_PAIRED_FILES ?? "null") as unknown;
  if (!workspace || !Array.isArray(files) || files.some(x => typeof x !== "string"))
    throw new Error("Missing RC7 owned read-only fixture identity");
  pi.on("tool_call", async (event, ctx) => {
    if (["recover_evidence", "search_evidence"].includes(event.toolName)) return;
    if (event.toolName === "read" && permittedRead(event.input, workspace, files)) return;
    ctx.abort();
    return { block: true, reason: "RC7 controlled read-only fixture: tool or path not permitted" };
  });
}
