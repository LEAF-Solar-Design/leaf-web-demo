/** Fail-closed production posture for the long-lived harness sidecar. */

function flagOn(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
}

export type AuthorSandboxProvider = "off" | "e2b";

export function authoredExecutionEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return flagOn(env.LEAF_AUTHORED_EXECUTION);
}

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

export function validateProductionHarnessEnv(env: NodeJS.ProcessEnv = process.env): void {
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
  if (authoredExecutionEnabled(env) &&
      (env.LEAF_AUTHOR_SANDBOX_PROVIDER ?? "").trim().toLowerCase() !== "e2b") {
    throw new Error(
      "production authored execution requires LEAF_AUTHOR_SANDBOX_PROVIDER=e2b",
    );
  }
  if (authoredExecutionEnabled(env)) {
    if (!(env.E2B_API_KEY ?? "").trim() && !(env.E2B_API_KEY_FILE ?? "").trim()) {
      throw new Error("production author sandbox requires an E2B credential source");
    }
    const brokerHost = (env.LEAF_SANDBOX_BROKER_HOST ?? "").trim().toLowerCase();
    if (!brokerHost || brokerHost === "httpbingo.org") {
      throw new Error("production author sandbox requires an explicit broker gateway host");
    }
  }
}
