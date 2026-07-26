/**
 * Hermetic unit test for E2bAgentRunner (lane 2A — the author-session E2B seam).
 *
 * NO real E2B, NO network, NO Anthropic auth: a FAKE sandbox factory is injected via the
 * runner's `sandboxFactory` option. The fake captures the create options (so we assert the
 * egress lock is configured) and returns canned stdout for the in-sandbox program (so we
 * assert the runner's harness-side assembly / validation / enforcement / secret discipline).
 *
 * The real in-sandbox Python (egress probe + structured author) is exercised by the LIVE
 * E2B smoke that writes docs/e2b-author-runner-receipt.json — this file stays hermetic.
 */

import { describe, expect, it } from "vitest";

import {
  canonicalJsonBytes,
  E2bAgentRunner,
} from "../src/ports/impl/e2bAgentRunner.js";
import type {
  SandboxCommandResult,
  SandboxCreateOptions,
  SandboxInfo,
  SandboxLike,
  SandboxNetworkInfo,
} from "../src/ports/impl/e2bAgentRunner.js";
import { validateToolPackage } from "../src/registry/toolPackageSchema.js";
import type {
  AgentGrant,
  AgentRunInput,
  AuthorToolset,
  FsTenantRepoTool,
  ResultEnvelope,
  ToolPackage,
  ValidationResult,
} from "../src/ports/index.js";

const BROKER_HOST = "broker.example.test";
const E2B_KEY = "e2b_SECRET_KEY_do_not_leak_123456789";
const GRANT_TOKEN = "sk-ant-oat01-FAKE-grant-do-not-leak";
const GRANT: AgentGrant = { kind: "oauth", oauthToken: GRANT_TOKEN };

/** A passing egress-lock network config (broker-only out, deny-all default). */
function lockedNetwork(): SandboxNetworkInfo {
  return { allowOut: [BROKER_HOST], denyOut: ["0.0.0.0/0"], allowPublicTraffic: false };
}

/** Canned stdout for the in-sandbox program: egress proven + a structured author result. */
function cannedStdout(over: {
  brokerReached?: boolean;
  blocked?: Record<string, { blocked: boolean; error_type?: string; status?: number }>;
} = {}): string {
  return JSON.stringify({
    broker: { reached: over.brokerReached ?? true, status: over.brokerReached === false ? null : 200 },
    blocked: over.blocked ?? {
      "http://169.254.169.254/latest/meta-data/": { blocked: true, error_type: "URLError" },
      "http://127.0.0.1:8130/": { blocked: true, error_type: "URLError" },
      "http://10.0.0.1/": { blocked: true, error_type: "URLError" },
      "http://172.16.0.1/": { blocked: true, error_type: "URLError" },
      "http://192.168.0.1/": { blocked: true, error_type: "URLError" },
      "https://example.com/": { blocked: true, error_type: "URLError" },
      "https://api.github.com/": { blocked: true, error_type: "URLError" },
      "https://1.1.1.1/": { blocked: true, error_type: "URLError" },
    },
    author: {
      name: "count-entities-per-layer",
      engine_op: "count_by_layer",
      description: "count entities per layer",
      params_schema: { type: "object", properties: { layer: { type: "string" } }, required: [] },
      returns_schema: { type: "object" },
      capabilities: ["drawing.read"],
      code: '"""authored in sandbox"""\n\n\ndef run(intake, params):\n    return ({"counts": {}}, None)\n',
      preview_verb: "counts entities per layer",
    },
  });
}

class FakeSandbox implements SandboxLike {
  readonly sandboxId = "sbx-fake-abc123";
  lastCommand: string | null = null;
  killed = false;
  constructor(private readonly cfg: { network: SandboxNetworkInfo; stdout: string; exitCode?: number }) {}
  async getInfo(): Promise<SandboxInfo> {
    return { network: this.cfg.network };
  }
  commands = {
    run: async (cmd: string): Promise<SandboxCommandResult> => {
      this.lastCommand = cmd;
      return { stdout: this.cfg.stdout, exitCode: this.cfg.exitCode ?? 0 };
    },
  };
  async kill(): Promise<boolean> {
    this.killed = true;
    return true;
  }
}

function makeFactory(cfg: { network: SandboxNetworkInfo; stdout: string; exitCode?: number }) {
  const captured: { createOpts?: SandboxCreateOptions; sandbox?: FakeSandbox } = {};
  const factory = async (opts: SandboxCreateOptions): Promise<SandboxLike> => {
    captured.createOpts = opts;
    const sb = new FakeSandbox(cfg);
    captured.sandbox = sb;
    return sb;
  };
  return { factory, captured };
}

function makeToolset() {
  const files = new Map<string, string>();
  const apsTestCalls: { tool: ToolPackage; params?: Record<string, unknown> }[] = [];
  const fsTenantRepo: FsTenantRepoTool = {
    root: "/mem",
    readFile: (p) => {
      const v = files.get(p);
      if (v === undefined) throw new Error(`no such file: ${p}`);
      return v;
    },
    writeFile: (p, c) => void files.set(p, c),
    exists: (p) => files.has(p),
    listDir: () => [...files.keys()],
  };
  const toolset: AuthorToolset = {
    fsTenantRepo,
    validateTool: (tool): ValidationResult => {
      const diagnostics = validateToolPackage(tool);
      return { ok: diagnostics.length === 0, diagnostics };
    },
    apsTestRun: async (tool, params): Promise<ResultEnvelope> => {
      apsTestCalls.push({ tool, params });
      return {
        ok: true,
        tool: tool.name,
        version: tool.version,
        result: { counts: {} },
        overlay: null,
        timing_ms: 1,
        cost: null,
        error: null,
      };
    },
  };
  return { toolset, files, apsTestCalls };
}

function makeInput(toolset: AuthorToolset, description = "count entities per layer"): AgentRunInput {
  return { description, systemPrompt: "author a tool", repoDir: "/mem", grant: GRANT, toolset };
}

describe("E2bAgentRunner — egress-locked author session (hermetic, fake sandbox)", () => {
  it("shares the canonical Unicode and key-order representation", () => {
    const bytes = canonicalJsonBytes({
      z: "雪", tiny: 1e-5, a: { β: 1, a: "é" }, n: 1.0,
    });
    expect(bytes.toString("utf8")).toBe(
      '{"a":{"a":"é","β":1.0000000000000000e+0},' +
      '"n":1.0000000000000000e+0,"tiny":1.0000000000000001e-5,"z":"雪"}',
    );
  });

  it("binds the HTTPS probe URL to the one allowlisted egress host", () => {
    expect(() => new E2bAgentRunner({
      brokerHost: "broker.internal",
      brokerProbeUrl: "https://other.internal/api/health",
    })).toThrow(/hostname must match/);
    expect(() => new E2bAgentRunner({
      brokerHost: "broker.internal",
      brokerProbeUrl: "http://broker.internal/api/health",
    })).toThrow(/must use HTTPS/);
    expect(() => new E2bAgentRunner({
      brokerHost: "broker.internal",
      brokerProbeUrl: "https://user:secret@broker.internal/api/health",
    })).toThrow(/cannot contain credentials/);
    expect(() => new E2bAgentRunner({
      brokerHost: "broker.internal",
      brokerProbeUrl: "https://broker.internal/api/health",
    })).not.toThrow();
  });

  it("boots ONE egress-locked sandbox and returns a valid CONTRACT §2 tool package", async () => {
    const { factory, captured } = makeFactory({ network: lockedNetwork(), stdout: cannedStdout() });
    const { toolset, files, apsTestCalls } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });

    const res = await runner.run(makeInput(toolset));

    // The egress lock was configured on the sandbox exactly like the proven receipt.
    const net = captured.createOpts?.network;
    expect(net?.allowOut).toEqual([BROKER_HOST]);
    expect(net?.denyOut).toEqual(["0.0.0.0/0"]);
    expect(net?.allowPublicTraffic).toBe(false);
    expect(captured.createOpts?.secure).toBe(true);
    expect(captured.createOpts?.apiKey).toBe(E2B_KEY); // key read + passed to the factory
    expect(captured.createOpts?.template).toBe("leaf-python-2026-07-23");
    expect(captured.createOpts?.metadata?.policy).toBe("leaf.sandbox-policy.v1");

    // The returned package is real + valid; the source round-tripped from the sandbox.
    expect(validateToolPackage(res.tool)).toEqual([]);
    expect(res.tool.engine_op).toBe("count_by_layer");
    expect(res.files).toEqual(["tools/count-entities-per-layer/tool.json", "tools/count-entities-per-layer/tool.py"]);
    expect(files.get("tools/count-entities-per-layer/tool.py")).toContain("def run(intake, params)");
    expect(files.get("tools/count-entities-per-layer/tool.json")).toBeTruthy();
    expect(res.preview).toContain("sbx-fake-abc123"); // preview cites the sandbox

    // Test-run went through the (fake) broker exactly once; sandbox was torn down.
    expect(apsTestCalls).toHaveLength(1);
    expect(captured.sandbox?.killed).toBe(true);

    // The receipt records a PASSING egress lock.
    const r = runner.lastSandbox;
    expect(r?.passed).toBe(true);
    expect(r?.broker.reached).toBe(true);
    expect(r?.everyDeniedProbeBlocked).toBe(true);
    expect(r?.configuredDenyAll).toBe(true);
    expect(r?.configuredBrokerOnly).toBe(true);
    expect(r?.authored).toEqual({ name: "count-entities-per-layer", engine_op: "count_by_layer" });
    expect(r?.boundary).toBe("author");
    expect(r?.tenantHash).toMatch(/^[a-f0-9]{64}$/);
    expect(r?.sourceHash).toMatch(/^[a-f0-9]{64}$/);
    expect(r?.resultHash).toMatch(/^[a-f0-9]{64}$/);
    expect(r?.policyVersion).toBe("leaf.sandbox-policy.v1");
    expect(r?.resourceUse).toMatchObject({
      cpuSecondsLimit: 30,
      memoryBytesLimit: 512 * 1024 * 1024,
      processLimit: 32,
      diskBytesLimit: 128 * 1024 * 1024,
      outputBytesLimit: 1024 * 1024,
    });
  });

  it("embeds the tenant description into the in-sandbox program (safely, base64) — not run in this PID", async () => {
    const { factory, captured } = makeFactory({ network: lockedNetwork(), stdout: cannedStdout() });
    const { toolset } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });
    const description = 'weird "quoted" desc; print("pwned")';

    await runner.run(makeInput(toolset, description));

    const cmd = captured.sandbox?.lastCommand ?? "";
    expect(cmd).toContain("base64.b64decode"); // program is base64-wrapped for the shell
    const m = cmd.match(/b64decode\('([^']+)'\)/);
    expect(m).toBeTruthy();
    const program = Buffer.from(m![1]!, "base64").toString("utf8");
    // The description reaches the sandbox as a JSON string literal (no shell/py breakout).
    expect(program).toContain(`DESCRIPTION = ${JSON.stringify(description)}`);
    // The runner itself never executes the authored tool.py — it only writes it to the repo.
    expect(program).toContain("def to_kebab");
    expect(program).toContain("169.254.169.254");
    expect(cmd).toContain("ulimit -t 30");
  });

  it("ENFORCES the lock: an off-allowlist target that gets through THROWS and writes nothing", async () => {
    const leaky = cannedStdout({
      blocked: {
        "https://example.com/": { blocked: false, status: 200 }, // leaked!
        "https://api.github.com/": { blocked: true, error_type: "URLError" },
      },
    });
    const { factory } = makeFactory({ network: lockedNetwork(), stdout: leaky });
    const { toolset, files, apsTestCalls } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });

    await expect(runner.run(makeInput(toolset))).rejects.toThrow(/egress lock/i);
    expect(files.size).toBe(0); // no tool written when the lock did not hold
    expect(apsTestCalls).toHaveLength(0);
    expect(runner.lastSandbox?.passed).toBe(false);
  });

  it("ENFORCES the lock: a mis-configured network (missing deny-all) fails even if probes were blocked", async () => {
    const { factory } = makeFactory({
      network: { allowOut: [BROKER_HOST], denyOut: [], allowPublicTraffic: false }, // no 0.0.0.0/0
      stdout: cannedStdout(),
    });
    const { toolset, files } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });

    await expect(runner.run(makeInput(toolset))).rejects.toThrow(/egress lock/i);
    expect(files.size).toBe(0);
    expect(runner.lastSandbox?.configuredDenyAll).toBe(false);
  });

  it("ENFORCES the lock: broker unreachable fails the run", async () => {
    const { factory } = makeFactory({ network: lockedNetwork(), stdout: cannedStdout({ brokerReached: false }) });
    const { toolset } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });
    await expect(runner.run(makeInput(toolset))).rejects.toThrow(/egress lock/i);
    expect(runner.lastSandbox?.broker.reached).toBe(false);
  });

  it("probeEgress() proves the lock without any toolset or tenant repo", async () => {
    const { factory, captured } = makeFactory({ network: lockedNetwork(), stdout: cannedStdout() });
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });

    const receipt = await runner.probeEgress();
    expect(receipt.passed).toBe(true);
    expect(receipt.authored).toBeNull(); // probe-only: no tool authored
    expect(receipt.brokerHost).toBe(BROKER_HOST);
    expect(captured.sandbox?.killed).toBe(true);
  });

  it("SECRET DISCIPLINE: neither the E2B key nor the tenant grant appears in the result or receipt", async () => {
    const { factory } = makeFactory({ network: lockedNetwork(), stdout: cannedStdout() });
    const { toolset } = makeToolset();
    const runner = new E2bAgentRunner({ brokerHost: BROKER_HOST, apiKey: E2B_KEY, sandboxFactory: factory });

    const res = await runner.run(makeInput(toolset));
    const serialized = JSON.stringify(res) + JSON.stringify(runner.lastSandbox);
    expect(serialized).not.toContain(E2B_KEY);
    expect(serialized).not.toContain(GRANT_TOKEN);
  });

  it("fails closed before parsing sandbox output above the fixed cap", async () => {
    const { factory } = makeFactory({
      network: lockedNetwork(),
      stdout: "x".repeat(1024 * 1024 + 1),
    });
    const { toolset } = makeToolset();
    const runner = new E2bAgentRunner({
      brokerHost: BROKER_HOST,
      apiKey: E2B_KEY,
      sandboxFactory: factory,
    });
    await expect(runner.run(makeInput(toolset))).rejects.toThrow(/output exceeded/i);
  });
});
