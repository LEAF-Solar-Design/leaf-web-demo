import { describe, expect, it, vi } from "vitest";

import { AUTHOR_SYSTEM_PROMPT } from "../src/agent/systemPrompt.js";
import {
  AUTHOR_RUNNER_GUIDE,
  completeRequiredBrokerTest,
} from "../src/ports/impl/agentSdkRunner.js";
import type { AuthorBrokerTestState } from "../src/ports/impl/agentSdkRunner.js";
import type { ResultEnvelope, ToolPackage } from "../src/ports/index.js";

const TOOL = {
  name: "add-footprint",
  version: "1.0.0",
  description: "Add a closed footprint",
  kind: "script",
  engine_op: "add_footprint",
  entry: "tools/add-footprint/tool.py",
  params: { type: "object", properties: {} },
  returns: { type: "object" },
  capabilities: ["drawing.write"],
  provenance: { author: "agent", created: "2026-07-30T00:00:00Z" },
} as ToolPackage;

function envelope(ok: boolean, errorCode?: string): ResultEnvelope {
  return {
    ok,
    tool: TOOL.name,
    version: TOOL.version,
    result: {},
    overlay: null,
    timing_ms: 1,
    cost: null,
    degraded_mode: false,
    error: errorCode
      ? { error_code: errorCode, message: "untrusted broker detail", retryable: false }
      : null,
  };
}

describe("Agent SDK author contract", () => {
  it("requires model-visible validation and a passing broker test", () => {
    expect(AUTHOR_SYSTEM_PROMPT).toContain("MUST validate the candidate");
    expect(AUTHOR_SYSTEM_PROMPT).toContain("receive ok:true before finishing");
    expect(AUTHOR_RUNNER_GUIDE).toContain('result["mutations"]["added"]');
    expect(AUTHOR_RUNNER_GUIDE).toContain("closed 2D drawing footprints");
  });

  it("runs a trusted default broker test when the model stops after validation", async () => {
    const run = vi.fn(async () => envelope(true));
    const source = "def run(intake, params):\n    return ({}, None)\n";
    await expect(
      completeRequiredBrokerTest(TOOL, run, undefined, source),
    ).resolves.toBeNull();
    expect(run).toHaveBeenCalledOnce();
    expect(run).toHaveBeenCalledWith(TOOL, {}, source);
  });

  it("fails closed with only a bounded broker error code", async () => {
    const run = vi.fn(async () => envelope(false, "BAD_PARAMS"));
    await expect(completeRequiredBrokerTest(TOOL, run)).rejects.toThrow(
      "authored tool failed required broker test (BAD_PARAMS): broker test returned ok:false.",
    );
  });

  it("does not hide a failed model broker test with a fallback retry", async () => {
    const run = vi.fn(async () => envelope(true));
    const failed: AuthorBrokerTestState = {
      attempted: true,
      ok: false,
      receipt: null,
      failureReason: "broker test returned ok:false",
      errorCode: "BAD_PARAMS",
    };
    await expect(completeRequiredBrokerTest(TOOL, run, failed)).rejects.toThrow(
      "authored tool failed required broker test (BAD_PARAMS)",
    );
    expect(run).not.toHaveBeenCalled();
  });

  it("does not repeat a broker test that the model already passed", async () => {
    const run = vi.fn(async () => envelope(false, "SHOULD_NOT_RUN"));
    const passed: AuthorBrokerTestState = {
      attempted: true,
      ok: true,
      receipt: null,
      failureReason: null,
      errorCode: null,
    };
    await expect(completeRequiredBrokerTest(TOOL, run, passed)).resolves.toBeNull();
    expect(run).not.toHaveBeenCalled();
  });
});
