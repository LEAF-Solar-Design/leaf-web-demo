import { describe, expect, it } from "vitest";

import { validateProductionHarnessEnv } from "../src/runtimeSafety.js";

const VALID = {
  LEAF_RUNTIME_ENV: "production",
  LEAF_AGENT_MOCK: "0",
  LEAF_HARNESS_AUTH: "1",
  LEAF_HARNESS_SECRET: "harness-secret",
  LEAF_BROKER_SECRET: "broker-secret",
};

describe("production harness runtime safety", () => {
  it("leaves non-production startup unchanged", () => {
    expect(() => validateProductionHarnessEnv({})).not.toThrow();
  });

  it("accepts the explicit production-safe contract", () => {
    expect(() => validateProductionHarnessEnv(VALID)).not.toThrow();
  });

  it("rejects any production mock-agent value other than exact zero", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AGENT_MOCK: "1" }))
      .toThrow(/LEAF_AGENT_MOCK=0/);
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AGENT_MOCK: undefined }))
      .toThrow(/LEAF_AGENT_MOCK=0/);
  });

  it("rejects disabled harness caller authentication", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_HARNESS_AUTH: "0" }))
      .toThrow(/LEAF_HARNESS_AUTH=1/);
  });

  it("rejects a missing harness secret", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_HARNESS_SECRET: " " }))
      .toThrow(/LEAF_HARNESS_SECRET/);
  });

  it("rejects a missing broker secret", () => {
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_BROKER_SECRET: "\n" }))
      .toThrow(/LEAF_BROKER_SECRET/);
  });
});
