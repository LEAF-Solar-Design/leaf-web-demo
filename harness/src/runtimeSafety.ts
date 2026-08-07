/** Fail-closed production posture for the long-lived harness sidecar. */

function flagOn(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
}

export type AuthorSandboxProvider = "off" | "e2b";
export type AuthorRunnerMode = "fake" | "agent-sdk";

export function authoredExecutionEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return flagOn(env.LEAF_AUTHORED_EXECUTION);
}

/**
 * Generated source is data until the broker executes it in E2B. The author model
 * stays on the trusted harness host so its tenant Claude grant never enters the
 * generated-code sandbox.
 */
export function authorRunnerMode(env: NodeJS.ProcessEnv = process.env): AuthorRunnerMode {
  return (env.LEAF_AGENT_MOCK ?? "").trim() === "1" ? "fake" : "agent-sdk";
}

/** @deprecated Kept only to parse legacy configuration during migration. */
export function authorSandboxProvider(env: NodeJS.ProcessEnv = process.env): AuthorSandboxProvider {
  const explicit = env.LEAF_AUTHOR_SANDBOX_PROVIDER;
  if (explicit === undefined) {
    return (env.LEAF_SANDBOX ?? "").trim().toLowerCase() === "e2b" ? "e2b" : "off";
  }
  const value = explicit.trim().toLowerCase();
  if (!value || value === "off") return "off";
  if (value === "e2b") return "e2b";
  throw new Error(`unsupported LEAF_AUTHOR_SANDBOX_PROVIDER: ${value}`);
}

/**
 * Posture-INDEPENDENT authored-execution sandbox boundary. Whenever authored
 * execution is EXPLICITLY armed -- in ANY posture, not only
 * LEAF_RUNTIME_ENV=production -- the tenant TOOL sandbox must be the broker's
 * E2B provider, the AUTHOR model must stay on the trusted harness host
 * (LEAF_AUTHOR_SANDBOX_PROVIDER off/unset: it processes the tenant grant, never
 * generated tenant code), and this harness must NOT hold an E2B credential (the
 * broker alone owns the tool sandbox). This is the harness half of the
 * cross-service fail-closed floor whose broker half lives in
 * broker.py::validate_runtime_safety; both refuse to start on a misconfigured
 * armed posture rather than run tenant-influenced work without isolation.
 */
export function assertAuthoredSandboxBoundary(env: NodeJS.ProcessEnv = process.env): void {
  if (!authoredExecutionEnabled(env)) return;
  if ((env.LEAF_TOOL_SANDBOX_PROVIDER ?? "").trim().toLowerCase() !== "e2b") {
    throw new Error(
      "authored execution requires LEAF_TOOL_SANDBOX_PROVIDER=e2b",
    );
  }
  if (
    (env.LEAF_AUTHOR_SANDBOX_PROVIDER ?? "").trim() &&
    (env.LEAF_AUTHOR_SANDBOX_PROVIDER ?? "").trim().toLowerCase() !== "off"
  ) {
    throw new Error(
      "authoring runs AgentSdkRunner on the trusted harness host; " +
      "LEAF_AUTHOR_SANDBOX_PROVIDER must be off or unset",
    );
  }
  if ((env.E2B_API_KEY ?? "").trim() || (env.E2B_API_KEY_FILE ?? "").trim()) {
    throw new Error(
      "the harness must not receive E2B credentials; the broker owns the tool sandbox",
    );
  }
}

export function validateProductionHarnessEnv(env: NodeJS.ProcessEnv = process.env): void {
  // The authored-execution sandbox boundary is enforced in EVERY posture (a
  // deliberately armed non-production harness must still isolate correctly).
  assertAuthoredSandboxBoundary(env);
  if ((env.LEAF_RUNTIME_ENV ?? "").trim().toLowerCase() !== "production") return;
  if ((env.LEAF_AGENT_MOCK ?? "").trim() !== "0") {
    throw new Error("production harness requires explicit LEAF_AGENT_MOCK=0");
  }
  if (!flagOn(env.LEAF_HARNESS_AUTH)) {
    throw new Error("production harness requires LEAF_HARNESS_AUTH=1");
  }
  if (!(env.LEAF_HARNESS_SECRET ?? "").trim()) {
    throw new Error("production harness requires nonblank LEAF_HARNESS_SECRET");
  }
  if (!(env.LEAF_BROKER_SECRET ?? "").trim()) {
    throw new Error("production harness requires nonblank LEAF_BROKER_SECRET");
  }
  if (!['0', '1'].includes((env.LEAF_AUTHORED_EXECUTION ?? '').trim())) {
    throw new Error("production harness requires explicit LEAF_AUTHORED_EXECUTION=0 or 1");
  }
  if (authoredExecutionEnabled(env)) {
    if ((env.LEAF_HARNESS_SESSION_STORE ?? "").trim().toLowerCase() !== "postgres") {
      throw new Error(
        "production authored execution requires LEAF_HARNESS_SESSION_STORE=postgres",
      );
    }
  }
}
