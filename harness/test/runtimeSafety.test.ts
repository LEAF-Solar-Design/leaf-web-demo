import { describe, expect, it } from "vitest";

import {
  authoredExecutionEnabled,
  authorSandboxProvider,
  validateProductionHarnessEnv,
} from "../src/runtimeSafety.js";

const VALID = {
  LEAF_RUNTIME_ENV: "production",
  LEAF_AGENT_MOCK: "0",
  LEAF_HARNESS_AUTH: "1",
  LEAF_HARNESS_SECRET: "harness-secret",
  LEAF_BROKER_SECRET: "broker-secret",
  LEAF_AUTHORED_EXECUTION: "0",
};

describe("production harness runtime safety", () => {
  it("defaults authored execution off in every runtime posture", () => {
    expect(authoredExecutionEnabled({})).toBe(false);
    expect(authoredExecutionEnabled({ LEAF_RUNTIME_ENV: "staging" })).toBe(false);
    expect(authoredExecutionEnabled({ LEAF_AUTHORED_EXECUTION: "1" })).toBe(true);
  });

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

  it("requires an explicit authored-execution selector in production", () => {
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: undefined,
    })).toThrow(/explicit LEAF_AUTHORED_EXECUTION/);
  });

  it("preserves legacy local author fallback with explicit-selector precedence", () => {
    expect(authorSandboxProvider({ LEAF_SANDBOX: "e2b" })).toBe("e2b");
    expect(authorSandboxProvider({
      LEAF_SANDBOX: "e2b",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "off",
    })).toBe("off");
    expect(authorSandboxProvider({
      LEAF_SANDBOX: "off",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toBe("e2b");
  });

  it("requires the E2B author provider before production authored execution", () => {
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_SANDBOX: "e2b",
    })).toThrow(/LEAF_AUTHOR_SANDBOX_PROVIDER=e2b/);
    expect(() => validateProductionHarnessEnv({ ...VALID, LEAF_AUTHORED_EXECUTION: "1" }))
      .toThrow(/LEAF_AUTHOR_SANDBOX_PROVIDER=e2b/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/credential source/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/broker gateway host/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_SANDBOX_BROKER_HOST: "broker.internal",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/probe URL/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_SANDBOX_BROKER_HOST: "broker.internal",
      LEAF_SANDBOX_BROKER_PROBE_URL: "https://other.internal/api/health",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/hostname must equal/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_SANDBOX_BROKER_HOST: "broker.internal",
      LEAF_SANDBOX_BROKER_PROBE_URL: "http://broker.internal/api/health",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/must use HTTPS/);
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_SANDBOX_BROKER_HOST: "broker.internal",
      LEAF_SANDBOX_BROKER_PROBE_URL: "https://broker.internal/api/health",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).not.toThrow();
  });

  it("requires PostgreSQL session authority for production authored execution", () => {
    const authored = {
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
      E2B_API_KEY: "test-key",
      LEAF_SANDBOX_BROKER_HOST: "broker.internal",
      LEAF_SANDBOX_BROKER_PROBE_URL: "https://broker.internal/api/health",
    };
    expect(() => validateProductionHarnessEnv({
      ...authored,
      LEAF_HARNESS_SESSION_STORE: "file",
    })).toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
    expect(() => validateProductionHarnessEnv(authored))
      .toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
  });
});
