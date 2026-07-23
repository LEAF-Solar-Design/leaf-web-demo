/** Fail-closed production posture for the long-lived harness sidecar. */

function flagOn(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
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
}
