/**
 * E2B AgentRunner — the egress-locked-sandbox implementation of the AgentRunner port
 * (lane 2A, the author-session half of the F2 keystone).
 *
 * WHY: the default `AgentSdkRunner` runs the design-time author session IN-PROCESS inside
 * the credential-holding Node harness. F2 (CRITICAL) is exactly that: tenant-influenced
 * authoring executes in a process that can see the harness's ambient environment. This
 * runner moves the UNTRUSTED authoring computation OUT of the harness PID into a real E2B
 * micro-VM whose network is locked to broker-only egress (mirrors the PROVEN receipt in
 * `docs/e2b-vendor-eval-receipt.json` / `harness/scripts/e2b-vendor-eval.mjs`).
 *
 * SHAPE SHIPPED (v1 — documented honestly, see docs/e2b-author-runner-receipt.json):
 *   "sandbox-boots-and-runs-the-author-step" — NOT a full Claude Agent SDK session inside
 *   the sandbox. On each run() this runner:
 *     1. Boots ONE E2B sandbox with the egress lock:
 *          network.allowOut = [<brokerHost>], denyOut = ["0.0.0.0/0"], allowPublicTraffic = false
 *          secure = true, apiKey from E2B_API_KEY env or C:/tmp/leaf-grants/e2b-api-key.txt.
 *     2. Runs ONE in-sandbox Python program that (a) PROVES broker-only reachability
 *        (the one allowlisted host answers; every off-allowlist target is blocked), and
 *        (b) runs the structured AUTHOR STEP — the same deterministic count/list/measure
 *        family the proven demo uses — emitting the tool.py source + manifest metadata.
 *        The code-generation happens INSIDE the egress-locked sandbox, never in this PID.
 *     3. Harness-side (deterministic, robust): assembles the CONTRACT §2 tool package from
 *        the sandbox's author output, writes tool.py + tool.json into the tenant checkout
 *        via the scoped `fsTenantRepo` tool, re-validates with the §2 oracle (defense in
 *        depth), and test-runs it once through the broker (aps_live=false). The authored
 *        tool.py is NEVER executed in this process (it runs later, zero-LLM, via the broker
 *        — the broker-side sandbox is lane 2B).
 *
 * The FULL SDK-in-sandbox variant (a live Claude session driving the three MCP tools inside
 * E2B, with the tenant grant injected into a scrubbed sandbox env) is the documented next
 * step; it is intentionally deferred because the SDK + per-tenant grant plumbing is heavy.
 * v1 makes the KEY security property true + testable: the author session's untrusted
 * execution happens in the egress-locked sandbox, never in the harness process.
 *
 * SECRET DISCIPLINE: the E2B API key is read from env/file at runtime and only ever flows
 * into the sandbox factory; it is never logged, printed, or returned. The tenant's Claude
 * grant (input.grant) is NOT read or injected in v1 (no live SDK in-sandbox yet) — the full
 * variant would inject it into a scrubbed sandbox env, never logged.
 *
 * The E2B SDK is loaded through a NON-LITERAL dynamic import specifier (mirrors
 * agentSdkRunner.ts) so `tsc` compiles this file WITHOUT e2b's heavy type graph, and the
 * hermetic unit test can inject a fake sandbox factory and never load e2b at all.
 */

import type {
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  Capability,
  ToolPackage,
} from "../index.js";
import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { createHash } from "node:crypto";

// --------------------------------------------------------------------------- //
// Minimal local view of the E2B Sandbox surface we rely on (documented, not
// exhaustive). Kept local so the gate never typechecks against e2b's .d.ts and so
// the hermetic test can implement a fake against the SAME typed seam.
// --------------------------------------------------------------------------- //
export interface SandboxNetworkInfo {
  allowOut?: string[];
  denyOut?: string[];
  allowPublicTraffic?: boolean;
}
export interface SandboxInfo {
  network?: SandboxNetworkInfo;
}
export interface SandboxCommandResult {
  stdout: string;
  stderr?: string;
  exitCode: number;
}
export interface SandboxLike {
  readonly sandboxId: string;
  getInfo(): Promise<SandboxInfo>;
  commands: { run(cmd: string, opts?: { timeoutMs?: number }): Promise<SandboxCommandResult> };
  kill(): Promise<unknown>;
}
export interface SandboxCreateOptions {
  apiKey: string;
  timeoutMs?: number;
  secure?: boolean;
  metadata?: Record<string, string>;
  template?: string;
  network?: { allowOut?: string[]; denyOut?: string[]; allowPublicTraffic?: boolean };
}
/** Factory for one egress-locked sandbox. Real = e2b's Sandbox.create; test = a fake. */
export type SandboxFactory = (opts: SandboxCreateOptions) => Promise<SandboxLike>;

/** Non-literal dynamic import so tsc treats the module as `any` (compiles w/o e2b types). */
function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

/** Default factory: boot a REAL e2b sandbox (operator-gated; requires a live E2B key). */
const defaultSandboxFactory: SandboxFactory = async (opts) => {
  const mod = (await dynImport(["e2b"])) as {
    Sandbox: {
      create(template: string, o: Omit<SandboxCreateOptions, "template">): Promise<SandboxLike>;
    };
  };
  const { template, ...createOpts } = opts;
  if (!template) throw new Error("E2B author sandbox template is required");
  return mod.Sandbox.create(template, createOpts);
};

// --------------------------------------------------------------------------- //
// Options + the egress receipt (exposed on the instance, like AgentSdkRunner.lastRun)
// --------------------------------------------------------------------------- //
export interface E2bAgentRunnerOptions {
  /** The ONE hostname allowed out of the sandbox (the broker). Everything else is denied. */
  brokerHost?: string;
  /** URL the in-sandbox probe POSTs to prove reachability. Defaults to https://<brokerHost>/post. */
  brokerProbeUrl?: string;
  /** Off-allowlist targets the probe must find BLOCKED (defaults mirror the vendor-eval receipt). */
  deniedTargets?: string[];
  /** Explicit E2B key (else E2B_API_KEY env, else the E2B_API_KEY_FILE / default key file). */
  apiKey?: string;
  /** Override the key file path (default C:\\tmp\\leaf-grants\\e2b-api-key.txt or $E2B_API_KEY_FILE). */
  apiKeyFile?: string;
  /** Sandbox wall-clock budget (ms). */
  timeoutMs?: number;
  /** In-sandbox command timeout (ms). */
  commandTimeoutMs?: number;
  /** Injected for the hermetic test; defaults to the real e2b factory. */
  sandboxFactory?: SandboxFactory;
}

/** Receipt proving the egress lock held for one sandbox boot (schema-stable). */
export interface EgressReceipt {
  schema: "leaf.e2b.author-runner.v1";
  startedAt: string;
  completedAt: string;
  stoppedAt: string;
  sandboxId: string;
  coldBootMs: number;
  brokerHost: string;
  network: { allowOut: string[]; denyOut: string[]; allowPublicTraffic?: boolean };
  broker: { host: string; url: string; reached: boolean; status: number | null };
  deniedProbes: Record<string, { blocked: boolean; error_type?: string; status?: number }>;
  configuredDenyAll: boolean;
  configuredBrokerOnly: boolean;
  everyDeniedProbeBlocked: boolean;
  passed: boolean;
  /** When run() authored a tool, a tiny summary (never the source). */
  authored: { name: string; engine_op: string } | null;
  boundary: "author";
  tenantHash: string;
  sourceHash: string | null;
  resultHash: string | null;
  templateVersion: string;
  policyVersion: string;
  stopReason: "completed";
  resourceUse: {
    wallMs: number;
    inputBytes: number;
    outputBytes: number;
    cpuSecondsLimit: number;
    memoryBytesLimit: number;
    processLimit: number;
    diskBytesLimit: number;
    fileLimit: number;
    outputBytesLimit: number;
    cpuSeconds: null;
    peakMemoryBytes: null;
    processes: null;
    diskBytes: null;
    files: null;
  };
}

const DEFAULT_BROKER_HOST = "httpbingo.org"; // proven public stand-in (see vendor-eval receipt)
const DEFAULT_DENIED_TARGETS = [
  "http://169.254.169.254/latest/meta-data/",
  "http://127.0.0.1:8130/",
  "http://10.0.0.1/",
  "http://172.16.0.1/",
  "http://192.168.0.1/",
  "https://example.com/",
  "https://api.github.com/",
  "https://1.1.1.1/",
];
const DEFAULT_KEY_FILE = "C:\\tmp\\leaf-grants\\e2b-api-key.txt";
const POLICY_VERSION = "leaf.sandbox-policy.v1";
const TEMPLATE_VERSION = "leaf-python-2026-07-23";
const LIMITS = Object.freeze({
  cpuSeconds: 30,
  memoryBytes: 512 * 1024 * 1024,
  processes: 32,
  diskBytes: 128 * 1024 * 1024,
  files: 128,
  outputBytes: 1024 * 1024,
  inputBytes: 512 * 1024,
});

function validatedProbeUrl(brokerHost: string, rawUrl: string): string {
  let probe: URL;
  try {
    probe = new URL(rawUrl);
  } catch {
    throw new Error("E2B broker probe URL is invalid");
  }
  if (probe.protocol !== "https:") {
    throw new Error("E2B broker probe URL must use HTTPS");
  }
  if (probe.username || probe.password) {
    throw new Error("E2B broker probe URL cannot contain credentials");
  }
  if (probe.hostname.toLowerCase() !== brokerHost.trim().toLowerCase()) {
    throw new Error("E2B broker probe hostname must match the allowlisted broker host");
  }
  return probe.toString();
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function canonicalText(value: unknown): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("canonical JSON rejects non-finite numbers");
    return value.toExponential(16).replace(/e([+-])0*(\d+)$/, "e$1$2");
  }
  if (Array.isArray(value)) return `[${value.map(canonicalText).join(",")}]`;
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort(
      (left, right) => Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")),
    );
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalText(record[key])}`).join(",")}}`;
  }
  throw new Error(`canonical JSON rejects ${typeof value}`);
}

export function canonicalJsonBytes(value: unknown): Buffer {
  return Buffer.from(canonicalText(value), "utf8");
}

// --------------------------------------------------------------------------- //
// Structured author family (mirrors the proven demo's count/list/measure family).
// This TEXT is the algorithm the SANDBOX selects + emits; it runs later zero-LLM.
// --------------------------------------------------------------------------- //
type AuthorOutput = {
  name: string;
  engine_op: string;
  description: string;
  params_schema: ToolPackage["params"];
  returns_schema: ToolPackage["returns"];
  capabilities: string[];
  code: string;
  preview_verb: string;
};
type ProbeOutput = {
  broker: { reached: boolean; status: number | null };
  blocked: Record<string, { blocked: boolean; error_type?: string; status?: number }>;
  author: AuthorOutput;
};

/**
 * The in-sandbox Python program: (1) probes egress (broker reachable + denied blocked),
 * (2) runs the structured author step. Standard-library only. The tenant `description` is
 * embedded as a JSON string literal (safe: JSON strings are valid Python string literals).
 */
function buildSandboxProgram(description: string, brokerUrl: string, deniedTargets: string[]): string {
  return `
import json, urllib.request, urllib.error, re

DESCRIPTION = ${JSON.stringify(description)}
BROKER_URL = ${JSON.stringify(brokerUrl)}
DENIED_TARGETS = ${JSON.stringify(deniedTargets)}

# ---- (1) egress probe: the ONE allowlisted host answers; everything else is blocked ----
broker_reached = False
broker_status = None
try:
    payload = json.dumps({"event": "author_session_booted", "tenant": "e2b-author-runner"}).encode()
    req = urllib.request.Request(
        BROKER_URL, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "leaf-e2b-author-runner/1"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        broker_status = getattr(resp, "status", None) or resp.getcode()
        broker_reached = 200 <= int(broker_status) < 300
except Exception:
    broker_reached = False

blocked = {}
for target in DENIED_TARGETS:
    try:
        with urllib.request.urlopen(target, timeout=5) as resp:
            blocked[target] = {"blocked": False, "status": int(getattr(resp, "status", 0) or resp.getcode())}
    except Exception as exc:
        blocked[target] = {"blocked": True, "error_type": type(exc).__name__}

# ---- (2) structured author step (runs INSIDE the sandbox; emits tool.py source) ----
def to_kebab(text):
    k = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    k = re.sub(r"-{2,}", "-", k)[:60].strip("-")
    return k or "authored-tool"

COUNT_CODE = '''"""Authored in an egress-locked E2B sandbox; runs with ZERO LLM against the Intake JSON."""


def run(intake, params):
    params = params or {}
    layer = params.get("layer")
    counts = {}
    for key in ("polylines", "inserts", "faces3d"):
        for ent in intake.get(key) or []:
            lay = ent.get("layer", "0")
            if layer and lay != layer:
                continue
            counts[lay] = counts.get(lay, 0) + 1
    return ({"counts": counts, "total": sum(counts.values())}, None)
'''

LIST_CODE = '''"""Authored in an egress-locked E2B sandbox; runs with ZERO LLM against the Intake JSON."""


def run(intake, params):
    params = params or {}
    prefix = params.get("prefix")
    layers = list(intake.get("layers") or [])
    if prefix:
        layers = [l for l in layers if l.startswith(prefix)]
    return ({"layers": layers, "count": len(layers)}, None)
'''

MEASURE_CODE = '''"""Authored in an egress-locked E2B sandbox; runs with ZERO LLM against the Intake JSON."""


def _shoelace(pts):
    n = len(pts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def run(intake, params):
    params = params or {}
    layer = params.get("layer")
    total_in2 = 0.0
    for pl in intake.get("polylines") or []:
        if not pl.get("closed"):
            continue
        if layer and pl.get("layer") != layer:
            continue
        total_in2 += _shoelace(pl.get("pts") or [])
    return ({"area_sqft": round(total_in2 / 144.0, 3),
             "units_assumption": "1 drawing unit = 1 inch; sqft = in2 / 144"}, None)
'''

d = (DESCRIPTION or "").lower()
if re.search(r"\\b(area|measure|square|sq\\.?\\s?ft|footage)\\b", d):
    author = {"engine_op": "measure_area_by_layer", "code": MEASURE_CODE,
              "params": {"type": "object", "properties": {"layer": {"type": "string", "description": "optional layer filter"}}, "required": []},
              "preview_verb": "measures closed-polyline area (sq ft)"}
elif re.search(r"\\b(list|names?)\\b", d) and not re.search(r"\\b(count|how many|number of)\\b", d):
    author = {"engine_op": "list_layers", "code": LIST_CODE,
              "params": {"type": "object", "properties": {"prefix": {"type": "string", "description": "optional layer-name prefix"}}, "required": []},
              "preview_verb": "lists layer names"}
else:
    author = {"engine_op": "count_by_layer", "code": COUNT_CODE,
              "params": {"type": "object", "properties": {"layer": {"type": "string", "description": "optional layer filter"}}, "required": []},
              "preview_verb": "counts entities per layer"}

out = {
    "broker": {"reached": broker_reached, "status": broker_status},
    "blocked": blocked,
    "author": {
        "name": to_kebab(DESCRIPTION),
        "engine_op": author["engine_op"],
        "description": DESCRIPTION,
        "params_schema": author["params"],
        "returns_schema": {"type": "object"},
        "capabilities": ["drawing.read"],
        "code": author["code"],
        "preview_verb": author["preview_verb"],
    },
}
print(json.dumps(out, sort_keys=True))
`;
}

/** Base64-wrap the program so no quoting/escaping survives the shell. */
function b64Command(program: string): string {
  const encoded = Buffer.from(program, "utf8").toString("base64");
  return `set -e; ulimit -t ${LIMITS.cpuSeconds}; ulimit -v ${LIMITS.memoryBytes / 1024}; ` +
    `ulimit -u ${LIMITS.processes}; ulimit -f ${LIMITS.diskBytes / 512}; ` +
    `ulimit -n ${LIMITS.files}; python3 -c "import base64; exec(base64.b64decode('${encoded}'))"`;
}

// --------------------------------------------------------------------------- //
// The runner
// --------------------------------------------------------------------------- //
export class E2bAgentRunner implements AgentRunner {
  /** Egress receipt from the most recent boot (read out of band by a driver/smoke). */
  lastSandbox: EgressReceipt | null = null;

  private readonly brokerHost: string;
  private readonly brokerProbeUrl: string;
  private readonly deniedTargets: string[];
  private readonly timeoutMs: number;
  private readonly commandTimeoutMs: number;
  private readonly factory: SandboxFactory;

  constructor(private readonly opts: E2bAgentRunnerOptions = {}) {
    this.brokerHost = opts.brokerHost ?? DEFAULT_BROKER_HOST;
    this.brokerProbeUrl = validatedProbeUrl(
      this.brokerHost,
      opts.brokerProbeUrl ?? `https://${this.brokerHost}/post`,
    );
    this.deniedTargets = opts.deniedTargets ?? DEFAULT_DENIED_TARGETS;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
    this.commandTimeoutMs = opts.commandTimeoutMs ?? 45_000;
    this.factory = opts.sandboxFactory ?? defaultSandboxFactory;
  }

  /** Resolve the E2B API key from env or file. NEVER logged; only flows to the factory. */
  private async resolveApiKey(): Promise<string> {
    const fromEnv = (this.opts.apiKey ?? process.env.E2B_API_KEY ?? "").trim();
    if (fromEnv) return fromEnv;
    const file = this.opts.apiKeyFile ?? process.env.E2B_API_KEY_FILE ?? DEFAULT_KEY_FILE;
    const key = (await readFile(file, "utf8")).trim();
    if (!key) throw new Error(`E2B API key is empty (env E2B_API_KEY unset and file empty: ${file})`);
    return key;
  }

  /** Boot ONE sandbox with the egress lock (broker-only out, deny-all default). */
  private async bootSandbox(): Promise<{ sandbox: SandboxLike; coldBootMs: number }> {
    const apiKey = await this.resolveApiKey();
    const started = performance.now();
    const sandbox = await this.factory({
      apiKey,
      timeoutMs: this.timeoutMs,
      secure: true,
      template: TEMPLATE_VERSION,
      metadata: { purpose: "leaf-author-session", policy: POLICY_VERSION },
      network: {
        allowOut: [this.brokerHost],
        denyOut: ["0.0.0.0/0"],
        allowPublicTraffic: false,
      },
    });
    return { sandbox, coldBootMs: Math.round(performance.now() - started) };
  }

  /** Run the in-sandbox probe (+author) and assemble the egress receipt. */
  private buildReceipt(
    info: SandboxInfo,
    probe: ProbeOutput,
    ctx: {
      sandboxId: string;
      coldBootMs: number;
      startedAt: string;
      authored: boolean;
      tenantHash?: string;
      inputBytes?: number;
    },
  ): EgressReceipt {
    const allowOut = info.network?.allowOut ?? [];
    const denyOut = info.network?.denyOut ?? [];
    const configuredDenyAll = denyOut.includes("0.0.0.0/0");
    const configuredBrokerOnly = allowOut.length === 1 && allowOut[0] === this.brokerHost;
    const everyDeniedProbeBlocked =
      this.deniedTargets.length > 0 &&
      this.deniedTargets.every((target) => probe.blocked[target]?.blocked === true);
    const passed =
      probe.broker.reached === true && everyDeniedProbeBlocked && configuredDenyAll && configuredBrokerOnly;
    const completedAt = new Date().toISOString();
    const outputBytes = Buffer.byteLength(JSON.stringify(probe), "utf8");
    return {
      schema: "leaf.e2b.author-runner.v1",
      startedAt: ctx.startedAt,
      completedAt,
      stoppedAt: completedAt,
      sandboxId: ctx.sandboxId,
      coldBootMs: ctx.coldBootMs,
      brokerHost: this.brokerHost,
      network: { allowOut, denyOut, allowPublicTraffic: info.network?.allowPublicTraffic },
      broker: { host: this.brokerHost, url: this.brokerProbeUrl, reached: probe.broker.reached, status: probe.broker.status },
      deniedProbes: probe.blocked,
      configuredDenyAll,
      configuredBrokerOnly,
      everyDeniedProbeBlocked,
      passed,
      authored: ctx.authored ? { name: probe.author.name, engine_op: probe.author.engine_op } : null,
      boundary: "author",
      tenantHash: ctx.tenantHash ?? sha256("probe"),
      sourceHash: ctx.authored ? sha256(probe.author.code) : null,
      resultHash: ctx.authored
        ? createHash("sha256").update(canonicalJsonBytes(probe.author)).digest("hex")
        : null,
      templateVersion: TEMPLATE_VERSION,
      policyVersion: POLICY_VERSION,
      stopReason: "completed",
      resourceUse: {
        wallMs: Date.parse(completedAt) - Date.parse(ctx.startedAt),
        inputBytes: ctx.inputBytes ?? 0,
        outputBytes,
        cpuSecondsLimit: LIMITS.cpuSeconds,
        memoryBytesLimit: LIMITS.memoryBytes,
        processLimit: LIMITS.processes,
        diskBytesLimit: LIMITS.diskBytes,
        fileLimit: LIMITS.files,
        outputBytesLimit: LIMITS.outputBytes,
        cpuSeconds: null,
        peakMemoryBytes: null,
        processes: null,
        diskBytes: null,
        files: null,
      },
    };
  }

  private parseProbe(result: SandboxCommandResult): ProbeOutput {
    if (result.exitCode !== 0) {
      throw new Error(`E2B in-sandbox program exited ${result.exitCode} (stderr redacted for secret-safety)`);
    }
    if (Buffer.byteLength(result.stdout, "utf8") > LIMITS.outputBytes) {
      throw new Error("E2B in-sandbox output exceeded the fixed limit");
    }
    const parsed = JSON.parse(result.stdout.trim()) as ProbeOutput;
    if (!parsed || typeof parsed !== "object" || !parsed.author || !parsed.broker || !parsed.blocked) {
      throw new Error("E2B in-sandbox program returned an unexpected shape");
    }
    return parsed;
  }

  /**
   * Boot an egress-locked sandbox, PROVE broker-only egress, tear it down, and return the
   * receipt. Used by the live smoke; also the exact lock every run() enforces. No toolset
   * or tenant repo required.
   */
  async probeEgress(description = "count entities per layer"): Promise<EgressReceipt> {
    const startedAt = new Date().toISOString();
    const { sandbox, coldBootMs } = await this.bootSandbox();
    try {
      const info = await sandbox.getInfo();
      const program = buildSandboxProgram(description, this.brokerProbeUrl, this.deniedTargets);
      const result = await sandbox.commands.run(b64Command(program), { timeoutMs: this.commandTimeoutMs });
      const probe = this.parseProbe(result);
      const receipt = this.buildReceipt(info, probe, {
        sandboxId: sandbox.sandboxId,
        coldBootMs,
        startedAt,
        authored: false,
      });
      this.lastSandbox = receipt;
      return receipt;
    } finally {
      await sandbox.kill().catch(() => false);
    }
  }

  /**
   * Spawn ONE design-time author session INSIDE an egress-locked E2B sandbox, author the
   * tool there, and return the frozen {tool, code, preview, files} shape. The untrusted
   * code-generation runs in the sandbox; the harness only does the deterministic write +
   * §2 validation + a broker test-run. Never on the run path.
   */
  async run(input: AgentRunInput): Promise<AgentRunResult> {
    this.lastSandbox = null;
    const startedAt = new Date().toISOString();
    const inputBytes = Buffer.byteLength(input.description, "utf8");
    if (inputBytes > LIMITS.inputBytes) {
      throw new Error("author request exceeded the fixed input limit");
    }
    // NOTE: input.grant is intentionally NOT read/injected in v1 (no live SDK in-sandbox
    // yet). The full SDK-in-sandbox variant would inject it into a SCRUBBED sandbox env,
    // never logged. This runner never touches the grant value.
    const { sandbox, coldBootMs } = await this.bootSandbox();
    let receipt: EgressReceipt;
    let probe: ProbeOutput;
    try {
      const info = await sandbox.getInfo();
      const program = buildSandboxProgram(input.description, this.brokerProbeUrl, this.deniedTargets);
      const result = await sandbox.commands.run(b64Command(program), { timeoutMs: this.commandTimeoutMs });
      probe = this.parseProbe(result);
      receipt = this.buildReceipt(info, probe, {
        sandboxId: sandbox.sandboxId,
        coldBootMs,
        startedAt,
        authored: true,
        tenantHash: sha256(input.repoDir),
        inputBytes,
      });
      this.lastSandbox = receipt;
    } finally {
      await sandbox.kill().catch(() => false);
    }

    // Enforce the egress lock: refuse to ship an author result from a sandbox that was not
    // provably broker-only. This is the property that makes 2A real, not decorative.
    if (!receipt.passed) {
      throw new Error(
        `E2B author sandbox failed the egress lock (brokerReached=${receipt.broker.reached}, ` +
          `everyDeniedBlocked=${receipt.everyDeniedProbeBlocked}, denyAll=${receipt.configuredDenyAll}, ` +
          `brokerOnly=${receipt.configuredBrokerOnly})`,
      );
    }

    // Harness-side, deterministic + robust: assemble the CONTRACT §2 package from the
    // sandbox's author output, write it into the tenant checkout, re-validate, test-run.
    const author = probe.author;
    const name = author.name;
    const submitted = input.toolset.submitTool({
      name,
      description: author.description,
      engine_op: author.engine_op,
      params: author.params_schema,
      returns: author.returns_schema,
      capabilities: author.capabilities.map(String) as Capability[],
      source: author.code,
      session: `e2b:${receipt.sandboxId}`,
    });

    // Test-run once through the broker (broker only, aps_live=false) to confirm the numbers.
    await input.toolset.apsTestRun(submitted.tool, {}, submitted.code);

    const preview =
      `Tool "${name}" ${author.preview_verb} (engine_op=${author.engine_op}); ` +
      `authored inside an egress-locked E2B sandbox (${receipt.sandboxId}); runs zero-LLM at runtime.`;
    return {
      tool: submitted.tool,
      code: submitted.code,
      preview,
      files: submitted.files,
      sourceReceipt: submitted.receipt,
    };
  }
}
