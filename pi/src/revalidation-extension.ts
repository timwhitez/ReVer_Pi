/** Opt-in development tool. Load explicitly; NOT enabled by the release default.
 * The registry and runner must be operator-owned, read-only mounts in a task
 * sandbox. Neither a successful exit nor the listed hashes certify a whole task.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { readFileSync, realpathSync } from "node:fs";
import { createHash, randomBytes } from "node:crypto";
import { resolve, delimiter, isAbsolute } from "node:path";

export default function registerRevalidation(pi: ExtensionAPI) {
  if (process.env.REVER_ENABLE_REVALIDATION !== "1") return;
  const pythonName = process.env.REVER_VERIFIER_PYTHON ?? "python3";
  const candidates = isAbsolute(pythonName) ? [pythonName] :
    (process.env.PATH ?? "").split(delimiter).filter(Boolean).map(p => resolve(p, pythonName));
  let python: string | undefined;
  for (const candidate of candidates) { try { python = realpathSync(candidate); break; } catch { /* Try next PATH component. */ } }
  if (!python) throw new Error("Verifier Python interpreter not found at registration");
  const interpreter = python;
  const interpreterSha = createHash("sha256").update(readFileSync(interpreter)).digest("hex");
  const registry = process.env.REVER_VERIFIER_REGISTRY;
  const registrySha = process.env.REVER_VERIFIER_REGISTRY_SHA256;
  const runner = process.env.REVER_VERIFIER_RUNNER;
  const runnerSha = process.env.REVER_VERIFIER_RUNNER_SHA256;
  if (!registry || !runner || !registrySha || !runnerSha ||
      !/^[a-f0-9]{64}$/.test(registrySha) || !/^[a-f0-9]{64}$/.test(runnerSha))
    throw new Error("Explicit immutable verifier registry and runner identities are required");
  const checkIdentity = () => {
    for (const [path, expected] of [[registry, registrySha], [runner, runnerSha], [interpreter, interpreterSha]])
      if (createHash("sha256").update(readFileSync(path)).digest("hex") !== expected)
        throw new Error("Verifier input identity changed");
  };
  checkIdentity();
  const maxCalls = Number(process.env.REVER_MAX_REVALIDATIONS ?? 3);
  if (!Number.isSafeInteger(maxCalls) || maxCalls < 1 || maxCalls > 10)
    throw new Error("Invalid revalidation quota");
  let used = 0;
  pi.registerTool({
    name:"revalidate_evidence", label:"Revalidate current task evidence",
    description:"Execute an operator-registered verifier by ID in the CURRENT task workspace. " +
      "Historical evidence is not current verification. No arbitrary commands are accepted.",
    parameters:Type.Object({verifier_id:Type.String({minLength:1,maxLength:128})},{additionalProperties:false}),
    async execute(_id, params, signal, _onUpdate, ctx) {
      try {
        if (++used > maxCalls) throw new Error("Revalidation quota exhausted");
        checkIdentity();
        const nonce = randomBytes(32).toString("hex");
        const workspace = realpathSync(ctx.cwd);
        const workspaceSha = createHash("sha256").update(workspace, "utf8").digest("hex");
        const result = await pi.exec(interpreter, ["-I", runner,
          "--registry",registry,"--registry-sha256",registrySha,"--workspace",workspace,
          "--verifier-id",params.verifier_id,"--invocation-nonce",nonce], {signal,timeout:610000});
        if (result.code !== 0 || result.killed) throw new Error("Registered verifier runner did not complete");
        checkIdentity();
        const receipt = JSON.parse(result.stdout);
        if (receipt.schema !== "reverpi.verification-receipt.v1" || receipt.verifier_id !== params.verifier_id ||
            receipt.invocation_nonce !== nonce || receipt.workspace_path_sha256 !== workspaceSha ||
            receipt.receipt_is_cryptographic_attestation !== false || typeof receipt.passed !== "boolean" ||
            !["completed", "timeout", "cancelled", "output_limit"].includes(receipt.status) ||
            (receipt.passed && (receipt.status !== "completed" || receipt.returncode !== 0 || receipt.observed_inputs_stable !== true)) ||
            receipt.dependency_completeness_certified !== false || receipt.decision_sufficiency_certified !== false)
          throw new Error("Invalid verification receipt");
        return {content:[{type:"text" as const,text:JSON.stringify(receipt)}],
          details:{current_observation:true,verifier_id:params.verifier_id,task_proof:false}};
      } catch (error) {
        // Do not disclose registry paths, subprocess errors, or private env values.
        // Throwing is Pi's failure signal; a returned isError field is ignored.
        // A valid receipt that reports passed:false is a result, not a failure.
        throw new Error(JSON.stringify({error:"revalidation_failed",
          exception:error instanceof Error ? error.name : "Error"}));
      }
    }
  });
}
