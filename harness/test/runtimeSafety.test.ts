import { describe, expect, it } from "vitest";

import {
  assertAuthoredSandboxBoundary,
  authoredExecutionEnabled,
  authorRunnerMode,
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

  it("always selects AgentSdkRunner outside the explicit fake mode", () => {
    expect(authorRunnerMode({})).toBe("agent-sdk");
    expect(authorRunnerMode({ LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b" })).toBe("agent-sdk");
    expect(authorRunnerMode({ LEAF_AGENT_MOCK: "1" })).toBe("fake");
  });

  it("keeps the Claude grant on the harness and the E2B credential on the broker", () => {
    const authored = {
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_HARNESS_SESSION_STORE: "postgres",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => validateProductionHarnessEnv(authored)).not.toThrow();
    expect(() => validateProductionHarnessEnv({
      ...authored,
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toThrow(/trusted harness host/);
    expect(() => validateProductionHarnessEnv({
      ...authored,
      E2B_API_KEY: "must-belong-to-broker",
    })).toThrow(/must not receive E2B credentials/);
  });

  it("requires PostgreSQL session authority for production authored execution", () => {
    const authored = {
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => validateProductionHarnessEnv({
      ...authored,
      LEAF_HARNESS_SESSION_STORE: "file",
    })).toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
    expect(() => validateProductionHarnessEnv(authored))
      .toThrow(/LEAF_HARNESS_SESSION_STORE=postgres/);
  });

  it("requires the broker E2B tool sandbox before production authored execution", () => {
    expect(() => validateProductionHarnessEnv({
      ...VALID,
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_HARNESS_SESSION_STORE: "postgres",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
  });
});

describe("authored-execution sandbox boundary is posture-independent", () => {
  it("does nothing when authored execution is not armed", () => {
    expect(() => assertAuthoredSandboxBoundary({})).not.toThrow();
    expect(() => assertAuthoredSandboxBoundary({ LEAF_RUNTIME_ENV: "staging" }))
      .not.toThrow();
  });

  it("refuses an armed NON-production harness with no tool sandbox provider", () => {
    // The old guard returned early for any non-production posture, so an armed
    // staging harness with no sandbox escaped entirely. It must now fail closed.
    expect(() => assertAuthoredSandboxBoundary({
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
    expect(() => validateProductionHarnessEnv({
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
    })).toThrow(/LEAF_TOOL_SANDBOX_PROVIDER=e2b/);
  });

  it("accepts an armed non-production harness with the correct boundary", () => {
    const armed = {
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
      LEAF_AUTHOR_SANDBOX_PROVIDER: "off",
    };
    expect(() => assertAuthoredSandboxBoundary(armed)).not.toThrow();
    expect(() => assertAuthoredSandboxBoundary({ ...armed, LEAF_AUTHOR_SANDBOX_PROVIDER: undefined }))
      .not.toThrow();
  });

  it("refuses an author sandbox provider or an E2B credential in any posture", () => {
    const armed = {
      LEAF_RUNTIME_ENV: "staging",
      LEAF_AUTHORED_EXECUTION: "1",
      LEAF_TOOL_SANDBOX_PROVIDER: "e2b",
    };
    expect(() => assertAuthoredSandboxBoundary({
      ...armed,
      LEAF_AUTHOR_SANDBOX_PROVIDER: "e2b",
    })).toThrow(/trusted harness host/);
    expect(() => assertAuthoredSandboxBoundary({
      ...armed,
      E2B_API_KEY: "must-belong-to-broker",
    })).toThrow(/must not receive E2B credentials/);
  });
});
