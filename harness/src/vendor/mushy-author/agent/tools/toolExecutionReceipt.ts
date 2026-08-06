import type {
  ResultEnvelope,
  ToolExecutionReceipt,
} from "../../ports/index.js";

const SHA256 = /^[a-f0-9]{64}$/;

function nonempty(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

export function parseToolExecutionReceipt(value: unknown): ToolExecutionReceipt | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const receipt = value as Record<string, unknown>;
  if (
    receipt.contract !== "leaf.tool-execution.v1" ||
    receipt.provider !== "e2b" ||
    receipt.isolation !== "microvm" ||
    receipt.passed !== true ||
    !nonempty(receipt.tenant_hash) ||
    !SHA256.test(receipt.tenant_hash) ||
    !nonempty(receipt.source_sha256) ||
    !SHA256.test(receipt.source_sha256) ||
    !nonempty(receipt.input_sha256) ||
    !SHA256.test(receipt.input_sha256) ||
    !nonempty(receipt.result_sha256) ||
    !SHA256.test(receipt.result_sha256) ||
    !nonempty(receipt.template_version) ||
    !nonempty(receipt.policy_version) ||
    !nonempty(receipt.started_at) ||
    !nonempty(receipt.stopped_at) ||
    !receipt.resource_use ||
    typeof receipt.resource_use !== "object" ||
    Array.isArray(receipt.resource_use)
  ) {
    return null;
  }
  return receipt as unknown as ToolExecutionReceipt;
}

export interface BrokerTestAcceptance {
  ok: boolean;
  receipt: ToolExecutionReceipt | null;
  reason?: string;
}

/**
 * A broker result is sufficient only when it is successful and, whenever the
 * E2B provider is selected, carries a complete verified execution receipt.
 */
export function acceptBrokerTestResult(
  envelope: ResultEnvelope,
  env: NodeJS.ProcessEnv = process.env,
): BrokerTestAcceptance {
  if (!envelope.ok) return { ok: false, receipt: null, reason: "broker test returned ok:false" };
  const receipt = parseToolExecutionReceipt(envelope.execution_provenance);
  const required = (env.LEAF_TOOL_SANDBOX_PROVIDER ?? "").trim().toLowerCase() === "e2b";
  if (required && !receipt) {
    return {
      ok: false,
      receipt: null,
      reason: "broker test did not return a valid E2B execution receipt",
    };
  }
  return { ok: true, receipt };
}
