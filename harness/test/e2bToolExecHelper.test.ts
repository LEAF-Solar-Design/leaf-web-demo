/**
 * Hermetic unit test for the e2b-tool-exec.mjs LAUNCHER (lane 2C, F2 v2).
 *
 * NO real E2B, NO network: a FAKE sandbox factory is injected via runJob's `factory`
 * parameter. The real e2b SDK is never loaded (defaultSandboxFactory dynamic-imports
 * "e2b" only when actually invoked, and these tests never invoke it).
 */

import { afterEach, beforeEach, describe, expect, it } from "vitest";

// @ts-expect-error — hermetic subject-under-test is a plain .mjs script (no type decls);
// runtime resolution works fine under vitest's esbuild/vite transform.
import { canonicalJsonBytes, probeScript, runJob } from "../scripts/e2b-tool-exec.mjs";

type NetworkInfo = { allowOut?: string[]; denyOut: string[]; allowPublicTraffic: boolean };
type RunCall = { cmd: string; opts: any };
type WriteCall = { path: string; data: string };

const JOB_DIR = "/home/user/leaf-job";
const GOOD_NETWORK: NetworkInfo = {
  allowOut: ["httpbingo.org"],
  denyOut: ["0.0.0.0/0"],
  allowPublicTraffic: false,
};

function baseJob() {
  return {
    schema: "leaf.e2b.tool-exec-job.v1",
    job: { source: "def run(i,p): return ({}, None)", intake: { polylines: [] }, params: {}, filename: "tool.py" },
    runner_py: "RUNNER-SENTINEL-PY",
    timeout_s: 7,
    broker_host: "httpbingo.org",
    denied_targets: ["https://example.com/", "https://api.github.com/", "https://1.1.1.1/"],
    probe_broker: false,
  };
}

function allBlockedProbeStdout(overrides?: Record<string, { blocked: boolean; error_type?: string }>): string {
  const blocked = {
    "https://example.com/": { blocked: true, error_type: "URLError" },
    "https://api.github.com/": { blocked: true, error_type: "URLError" },
    "https://1.1.1.1/": { blocked: true, error_type: "URLError" },
    ...overrides,
  };
  const metadataTarget = "http://169.254.169.254/latest/meta-data/";
  return JSON.stringify({
    blocked,
    platform_metadata: {
      target: metadataTarget,
      credential_material_present: false,
      attempts: Object.fromEntries([
        "no_auth_get",
        "aws_token_put",
        "aws_invalid_token_get",
        "gcp_flavor_get",
        "e2b_invalid_access_token_get",
        "invalid_bearer_get",
      ].map((label) => [label, { blocked: false, status: 401 }])),
    },
    tenant_network_namespace: {
      command_ok: true,
      blocked: {
        ...Object.fromEntries(Object.keys(blocked).map((target) => [
          target, { blocked: true, error_type: "URLError" },
        ])),
        [metadataTarget]: { blocked: true, error_type: "URLError" },
      },
    },
    broker_ok: null,
  });
}

function defaultExecStdout(): string {
  return JSON.stringify({ ok: true, ret: { form: "pair", result: { n: 1 }, overlay: null } });
}

class FakeSandbox {
  readonly sandboxId: string;
  readonly writes: WriteCall[] = [];
  readonly runs: RunCall[] = [];
  killCount = 0;

  private readonly network: NetworkInfo;
  private readonly probeStdout: string;
  private readonly execStdout: string;
  private readonly execExitCode: number;
  private readonly execThrows: Error | null;
  private readonly templateId: string;
  private readonly templateName: string;

  constructor(cfg: {
    sandboxId?: string;
    network?: NetworkInfo;
    probeStdout?: string;
    execStdout?: string;
    execExitCode?: number;
    execThrows?: Error | null;
    templateId?: string;
    templateName?: string;
  }) {
    this.sandboxId = cfg.sandboxId ?? "sbx-fake-e2b-001";
    this.network = cfg.network ?? GOOD_NETWORK;
    this.probeStdout = cfg.probeStdout ?? allBlockedProbeStdout();
    this.execStdout = cfg.execStdout ?? defaultExecStdout();
    this.execExitCode = cfg.execExitCode ?? 0;
    this.execThrows = cfg.execThrows ?? null;
    this.templateId = cfg.templateId ?? "r0kto3ypd1sgylx4tkz4";
    this.templateName = cfg.templateName ?? "leaf-python-2026-07-29-v2";
  }

  async getInfo() {
    return {
      network: this.network,
      templateId: this.templateId,
      name: this.templateName,
    };
  }

  files = {
    write: async (path: string, data: string) => {
      this.writes.push({ path, data });
    },
  };

  commands = {
    run: async (cmd: string, opts: any) => {
      this.runs.push({ cmd, opts });
      if (cmd.startsWith("python3 -c")) {
        return { exitCode: 0, stdout: this.probeStdout };
      }
      if (cmd.includes("runner.py")) {
        if (this.execThrows) throw this.execThrows;
        return { exitCode: this.execExitCode, stdout: this.execStdout };
      }
      throw new Error(`FakeSandbox: unexpected command: ${cmd}`);
    },
  };

  async kill() {
    this.killCount += 1;
    return true;
  }
}

function makeFactory(cfg: ConstructorParameters<typeof FakeSandbox>[0] = {}) {
  const captured: { opts?: any; sandbox?: FakeSandbox; invoked: boolean } = { invoked: false };
  const factory = async (opts: any) => {
    captured.invoked = true;
    captured.opts = opts;
    const sb = new FakeSandbox(cfg);
    captured.sandbox = sb;
    return sb;
  };
  return { factory, captured };
}

const ORIGINAL_E2B_API_KEY = process.env.E2B_API_KEY;
const ORIGINAL_E2B_API_KEY_FILE = process.env.E2B_API_KEY_FILE;

beforeEach(() => {
  process.env.E2B_API_KEY = "test-key-123";
  delete process.env.E2B_API_KEY_FILE;
});

afterEach(() => {
  if (ORIGINAL_E2B_API_KEY === undefined) delete process.env.E2B_API_KEY;
  else process.env.E2B_API_KEY = ORIGINAL_E2B_API_KEY;
  if (ORIGINAL_E2B_API_KEY_FILE === undefined) delete process.env.E2B_API_KEY_FILE;
  else process.env.E2B_API_KEY_FILE = ORIGINAL_E2B_API_KEY_FILE;
});

describe("e2b-tool-exec.mjs runJob — hermetic (fake sandbox factory)", () => {
  it("treats an HTTP response as reachable, not blocked", () => {
    const script = probeScript("httpbingo.org", ["https://example.com/"], false);
    expect(script).toContain("except HTTPError as exc");
    expect(script).toContain('{"blocked": False, "status": exc.code}');
  });

  it("requires transport blocking in the tenant network namespace", async () => {
    const envelope = baseJob();
    const unsafeProbeBody = JSON.parse(allBlockedProbeStdout());
    unsafeProbeBody.tenant_network_namespace.blocked[
      "http://169.254.169.254/latest/meta-data/"
    ] = { blocked: false, status: 401 };
    const unsafeProbe = JSON.stringify(unsafeProbeBody);
    const { factory } = makeFactory({ probeStdout: unsafeProbe });

    const result: any = await runJob(envelope, factory);

    expect(result.receipt?.platformMetadataSafe).toBe(false);
    expect(result.receipt?.passed).toBe(false);
    expect(result.result).toBeNull();
  });

  it("uses the cross-language canonical UTF-8 JSON representation", () => {
    const first = { z: "雪", tiny: 1e-5, a: { β: 1, a: "é" }, n: 1.0 };
    const reordered = { n: 1, a: { a: "é", β: 1 }, tiny: 1e-5, z: "雪" };
    expect(canonicalJsonBytes(first).toString("utf8"))
      .toBe(
        '{"a":{"a":"é","β":1.0000000000000000e+0},' +
        '"n":1.0000000000000000e+0,"tiny":1.0000000000000001e-5,"z":"雪"}',
      );
    expect(canonicalJsonBytes(reordered)).toEqual(canonicalJsonBytes(first));
  });

  it("uploads the job payload + runner via files.write, never via argv (>1MB intake)", async () => {
    const filler = "Q".repeat(1024 * 1024 + 17); // >1MB synthetic filler, distinctive char
    const envelope = baseJob();
    (envelope.job as any).intake = { polylines: [], filler };
    const { factory, captured } = makeFactory();

    const result: any = await runJob(envelope, factory);

    expect(result.helper_error).toBeNull();
    const sandbox = captured.sandbox!;
    expect(sandbox.writes).toHaveLength(2);

    const jobWrite = sandbox.writes.find((w) => w.path === `${JOB_DIR}/job.json`);
    expect(jobWrite).toBeTruthy();
    expect(JSON.parse(jobWrite!.data)).toEqual(envelope.job);

    const runnerWrite = sandbox.writes.find((w) => w.path === `${JOB_DIR}/runner.py`);
    expect(runnerWrite).toBeTruthy();
    expect(runnerWrite!.data).toBe("RUNNER-SENTINEL-PY");

    // The 1MB intake NEVER touches an argv / command string.
    expect(sandbox.runs.length).toBeGreaterThan(0);
    for (const run of sandbox.runs) {
      expect(run.cmd.length).toBeLessThan(8192);
      expect(run.cmd).not.toContain(filler);
      expect(run.cmd).not.toContain("Q".repeat(200));
    }
  });

  it("issues the exec command in the exact expected form with the job's timeout", async () => {
    const envelope = baseJob();
    const { factory, captured } = makeFactory();

    const result: any = await runJob(envelope, factory);

    expect(result.helper_error).toBeNull();
    const sandbox = captured.sandbox!;
    expect(sandbox.runs).toHaveLength(2);
    const execCall = sandbox.runs[1]!;
    expect(execCall.cmd).toBe(`python3 ${JOB_DIR}/runner.py < ${JOB_DIR}/job.json`);
    expect(execCall.opts?.timeoutMs).toBe(7000);
  });

  it("refuses to relay when the sandbox's reported network config mismatches the egress lock", async () => {
    const envelope = baseJob();
    const { factory } = makeFactory({
      network: { allowOut: ["evil.example"], denyOut: ["0.0.0.0/0"], allowPublicTraffic: false },
    });

    const result: any = await runJob(envelope, factory);

    expect(result.receipt?.passed).toBe(false);
    expect(result.result).toBeNull();
    expect(result.helper_error?.type).toBe("EgressLockFailed");
  });

  it("refuses to relay when the in-VM probe reports a denied target leaked through", async () => {
    const envelope = baseJob();
    const leakyProbe = allBlockedProbeStdout({ "https://example.com/": { blocked: false } });
    const { factory } = makeFactory({ network: GOOD_NETWORK, probeStdout: leakyProbe });

    const result: any = await runJob(envelope, factory);

    expect(result.receipt?.passed).toBe(false);
    expect(result.result).toBeNull();
    expect(result.helper_error?.type).toBe("EgressLockFailed");
  });

  it("refuses to relay when public traffic is reported enabled", async () => {
    const envelope = baseJob();
    const { factory } = makeFactory({
      network: { allowOut: ["httpbingo.org"], denyOut: ["0.0.0.0/0"], allowPublicTraffic: true },
    });

    const result: any = await runJob(envelope, factory);

    expect(result.receipt?.configuredNoPublicTraffic).toBe(false);
    expect(result.receipt?.passed).toBe(false);
    expect(result.result).toBeNull();
    expect(result.helper_error?.type).toBe("EgressLockFailed");
  });

  it("tears down the sandbox (kill) even when the exec command throws", async () => {
    const envelope = baseJob();
    const { factory, captured } = makeFactory({ execThrows: new Error("boom: exec failed") });

    const result: any = await runJob(envelope, factory);

    expect(result.result).toBeNull();
    expect(result.helper_error?.stage).toBe("exec");
    expect(captured.sandbox!.killCount).toBe(1);
  });

  it("resolves the API key from env/file and only invokes the factory once a key is found", async () => {
    delete process.env.E2B_API_KEY;
    process.env.E2B_API_KEY_FILE = `C:\\tmp\\e2b-key-does-not-exist-${Date.now()}.txt`;
    const { factory, captured } = makeFactory();

    const missing: any = await runJob(baseJob(), factory);

    expect(missing.helper_error?.stage).toBe("key");
    expect(captured.invoked).toBe(false);

    delete process.env.E2B_API_KEY_FILE;
    process.env.E2B_API_KEY = "env-key";
    const { factory: factory2, captured: captured2 } = makeFactory();

    const ok: any = await runJob(baseJob(), factory2);

    expect(ok.helper_error).toBeNull();
    expect(captured2.invoked).toBe(true);
    expect(captured2.opts?.apiKey).toBe("env-key");
  });

  it("contains the API key to the factory opts — never in uploads, commands, or the result blob", async () => {
    const envelope = baseJob();
    const { factory, captured } = makeFactory();

    const result: any = await runJob(envelope, factory);

    expect(captured.opts?.apiKey).toBe("test-key-123");

    const sandbox = captured.sandbox!;
    for (const w of sandbox.writes) {
      expect(w.data).not.toContain("test-key-123");
    }
    for (const r of sandbox.runs) {
      expect(r.cmd).not.toContain("test-key-123");
      expect(JSON.stringify(r.opts ?? {})).not.toContain("test-key-123");
    }
    expect(JSON.stringify(result)).not.toContain("test-key-123");
  });

  it("relays the runner's stdout verbatim as `result` and reports a passing receipt", async () => {
    const envelope = baseJob();
    const execStdout = defaultExecStdout();
    const { factory, captured } = makeFactory({ execStdout });

    const result: any = await runJob(envelope, factory);

    expect(result.result).toEqual(JSON.parse(execStdout));
    expect(result.receipt?.passed).toBe(true);
    expect(result.receipt?.sandboxId).toBe(captured.sandbox!.sandboxId);
    expect(captured.sandbox!.killCount).toBe(1);
  });

  it("enforces the no-network tool policy and emits the complete audit receipt", async () => {
    const envelope: any = baseJob();
    envelope.audit = {
      tenant_hash: "tenant-hash",
      source_hash: "source-hash",
      input_hash: "input-hash",
      job_hash: "job-hash",
      template_version: "leaf-python-2026-07-29-v2",
      template_id: "r0kto3ypd1sgylx4tkz4",
      template_build_id: "273367ae-6a5b-47da-ba46-7782c2fa5d6b",
      policy_version: "leaf.sandbox-policy.v1",
      limits: {
        source_bytes: 524288,
        input_bytes: 8388608,
        params_bytes: 262144,
        output_bytes: 1048576,
      },
    };
    const probeStdout = allBlockedProbeStdout({
      "https://httpbingo.org/": { blocked: true, error_type: "URLError" },
    });
    const { factory, captured } = makeFactory({
      // E2B omits allowOut from getInfo() when the configured allowlist is empty.
      network: { denyOut: ["0.0.0.0/0"], allowPublicTraffic: false },
      probeStdout,
    });

    const result: any = await runJob(envelope, factory);

    expect(captured.opts?.network.allowOut).toEqual([]);
    expect(captured.opts?.template).toBe("leaf-python-2026-07-29-v2");
    expect(captured.opts?.timeoutMs).toBe(112_000);
    expect(captured.sandbox?.runs[0]?.opts?.timeoutMs).toBe(45_000);
    expect(result.helper_error).toBeNull();
    expect(result.receipt).toMatchObject({
      passed: true,
      boundary: "tool",
      configuredNoPublicTraffic: true,
      tenantHash: "tenant-hash",
      sourceHash: "source-hash",
      inputHash: "input-hash",
      configuredTemplate: true,
      tenantNetworkNamespaceIsolated: true,
      templateVersion: "leaf-python-2026-07-29-v2",
      templateId: "r0kto3ypd1sgylx4tkz4",
      templateBuildId: "273367ae-6a5b-47da-ba46-7782c2fa5d6b",
      policyVersion: "leaf.sandbox-policy.v1",
      stopReason: "completed",
    });
    expect(result.receipt.resultHash).toMatch(/^[a-f0-9]{64}$/);
    expect(result.receipt.jobHash).toMatch(/^[a-f0-9]{64}$/);
    expect(result.receipt.resourceUse.outputBytes).toBeGreaterThan(0);
  });
});
