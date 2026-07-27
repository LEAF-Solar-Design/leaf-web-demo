import { describe, expect, it } from "vitest";

import {
  acceptBrokerTestResult,
  parseToolExecutionReceipt,
} from "../src/agent/tools/toolExecutionReceipt.js";
import type {
  ResultEnvelope,
  ToolExecutionReceipt,
} from "../src/ports/index.js";

const HASH = "a".repeat(64);
const RECEIPT: ToolExecutionReceipt = {
  contract: "leaf.tool-execution.v1",
  provider: "e2b",
  isolation: "microvm",
  passed: true,
  tenant_hash: HASH,
  source_sha256: HASH,
  input_sha256: HASH,
  result_sha256: HASH,
  template_version: "leaf-python-2026-07-23",
  policy_version: "leaf.sandbox-policy.v1",
  started_at: "2026-07-26T00:00:00Z",
  stopped_at: "2026-07-26T00:00:01Z",
  resource_use: { wallMs: 1000 },
};

function envelope(execution_provenance?: ToolExecutionReceipt): ResultEnvelope {
  return {
    ok: true,
    tool: "arrange-panels-as-cat",
    version: "1.0.0",
    result: {},
    overlay: null,
    timing_ms: 1,
    cost: null,
    error: null,
    ...(execution_provenance ? { execution_provenance } : {}),
  };
}

describe("broker tool execution receipt", () => {
  it("accepts a complete E2B receipt and rejects malformed hashes", () => {
    expect(parseToolExecutionReceipt(RECEIPT)).toEqual(RECEIPT);
    expect(parseToolExecutionReceipt({ ...RECEIPT, source_sha256: "short" })).toBeNull();
  });

  it("requires the receipt whenever the E2B tool provider is selected", () => {
    const env = { LEAF_TOOL_SANDBOX_PROVIDER: "e2b" };
    expect(acceptBrokerTestResult(envelope(RECEIPT), env)).toEqual({
      ok: true,
      receipt: RECEIPT,
    });
    expect(acceptBrokerTestResult(envelope(), env)).toMatchObject({
      ok: false,
      receipt: null,
      reason: expect.stringContaining("execution receipt"),
    });
  });

  it("keeps local broker tests compatible while still rejecting ok:false", () => {
    expect(acceptBrokerTestResult(envelope(), {})).toEqual({ ok: true, receipt: null });
    expect(acceptBrokerTestResult({ ...envelope(), ok: false }, {})).toMatchObject({
      ok: false,
      receipt: null,
    });
  });
});
