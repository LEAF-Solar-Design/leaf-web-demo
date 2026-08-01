import { describe, expect, it, vi } from "vitest";

import { AUTHOR_SYSTEM_PROMPT } from "../src/agent/systemPrompt.js";
import {
  AUTHOR_RUNNER_GUIDE,
  completeRequiredBrokerTest,
  resolveAuthorModel,
  sampleBrokerTestParams,
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
  it("uses the explicit, environment, then proven default model", () => {
    expect(resolveAuthorModel("claude-opus-4-1", { LEAF_SPINE_MODEL: "claude-haiku-4-5" })).toBe(
      "claude-opus-4-1",
    );
    expect(resolveAuthorModel(undefined, { LEAF_SPINE_MODEL: "claude-haiku-4-5" })).toBe(
      "claude-haiku-4-5",
    );
    expect(resolveAuthorModel(undefined, {})).toBe("claude-sonnet-5");
  });

  it("requires model-visible validation and a passing broker test", () => {
    expect(AUTHOR_SYSTEM_PROMPT).toContain("MUST validate the candidate");
    expect(AUTHOR_SYSTEM_PROMPT).toContain("receive ok:true before finishing");
    expect(AUTHOR_RUNNER_GUIDE).toContain('result["mutations"]["added"]');
    expect(AUTHOR_RUNNER_GUIDE).toContain("faceted closed planar polylines");
    expect(AUTHOR_RUNNER_GUIDE).toContain('result["mutations"]["removed"]');
    expect(AUTHOR_RUNNER_GUIDE).toContain("Live transforms");
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

  it("derives a bounded valid fallback input from required params", async () => {
    const parameterized = {
      ...TOOL,
      params: {
        type: "object",
        required: ["width", "label", "options"],
        properties: {
          width: { type: "number", minimum: 2 },
          label: { type: "string", minLength: 3 },
          options: {
            type: "object",
            required: ["centered"],
            properties: { centered: { type: "boolean" } },
          },
        },
      },
    } as ToolPackage;
    expect(sampleBrokerTestParams(parameterized.params)).toEqual({
      width: 2,
      label: "sample",
      options: { centered: true },
    });
    const run = vi.fn(async () => envelope(true));
    await completeRequiredBrokerTest(parameterized, run);
    expect(run).toHaveBeenCalledWith(
      parameterized,
      { width: 2, label: "sample", options: { centered: true } },
      undefined,
    );
  });

  it("fails closed on unsafe required fallback fields", () => {
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: ["__proto__"],
      properties: { "__proto__": { type: "string" } },
    })).toThrow("unsafe required field");
  });

  it("samples the safe drawing-write controls and optional defaults", () => {
    expect(sampleBrokerTestParams({
      type: "object",
      required: ["width"],
      properties: {
        width: { type: "integer", minimum: 1.5, maximum: 10 },
        drawing_id: { type: "string", default: "acceptance-drawing" },
        dry_run: { type: "boolean", default: false },
      },
    })).toEqual({ width: 2, drawing_id: "acceptance-drawing", dry_run: true });
  });

  it("fails closed instead of clamping or ignoring unsupported constraints", () => {
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: ["name"],
      properties: { name: { type: "string", minLength: 65 } },
    })).toThrow("minLength is outside the safe bound");
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: ["points"],
      properties: { points: { type: "array", minItems: 4, items: { type: "number" } } },
    })).toThrow("minItems is outside the safe bound");
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: ["name"],
      properties: { name: { type: "string", pattern: "^[A-Z]+$" } },
    })).toThrow("unsupported keyword pattern");
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: ["step"],
      properties: { step: { type: "number", multipleOf: 0.5 } },
    })).toThrow("unsupported keyword multipleOf");
    expect(() => sampleBrokerTestParams({
      type: "object",
      required: [42],
      properties: {},
    })).toThrow("required names must be strings");
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
