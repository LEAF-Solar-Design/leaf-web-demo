/**
 * e2b-tool-exec.mjs — the broker's e2b-microvm tier LAUNCHER (lane 2C, F2 v2).
 *
 * Runs ONE tenant-tool job inside a real, egress-locked E2B micro-VM and relays the
 * in-VM runner's JSON verbatim. Protocol (schemas frozen in the plan + mirrored by
 * server/tool_loader.py::_run_in_sandbox_e2b):
 *
 *   stdin  : leaf.e2b.tool-exec-job.v1
 *            { job:{source,intake,params,filename}, runner_py, timeout_s,
 *              broker_host, denied_targets, probe_broker }
 *   stdout : leaf.e2b.tool-exec-result.v1
 *            { receipt:{...passed}, result:<runner stdout, verbatim>|null,
 *              helper_error:{stage,type,msg}|null }   (exit 0 iff the blob was emitted)
 *
 * Security properties (mirrors the PROVEN harness/scripts/e2b-vendor-eval.mjs +
 * src/ports/impl/e2bAgentRunner.ts substrate):
 *  - Sandbox.create uses the exact proven network policy: allowOut broker-host-only,
 *    denyOut 0.0.0.0/0, allowPublicTraffic false, secure true.
 *  - REFUSAL RULE: if the egress receipt cannot be proven (config cross-check via
 *    getInfo() + in-VM denied-target probes [+ broker POST only when probe_broker]),
 *    the tool result is NEVER relayed (result stays null).
 *  - The E2B API key stops HERE (the trusted launcher): it goes only into the
 *    Sandbox.create opts — never into the VM (no `envs` on commands.run), never logged,
 *    never in the emitted blob.
 *  - The job payload (intake can be ~0.5 MB+) is uploaded via files.write — it NEVER
 *    touches a command line. The runner is executed via stdin redirect from the
 *    uploaded file, so tool_loader's _SANDBOX_RUNNER (PEP-578 audit-hook jail
 *    included) runs byte-for-byte inside the VM as defense-in-depth.
 *
 * The `factory` parameter is the hermetic-test seam (same pattern as
 * e2bAgentRunner.ts): tests inject a fake sandbox factory; the default factory
 * dynamically imports "e2b" only when actually invoked, so importing this module
 * never loads the SDK.
 */
import { readFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";

const SCHEMA_RESULT = "leaf.e2b.tool-exec-result.v1";
const DEFAULT_KEY_FILE = "C:\\tmp\\leaf-grants\\e2b-api-key.txt";
const JOB_DIR = "/home/user/leaf-job";
const PROBE_TIMEOUT_MS = 45_000;
const BOOT_BUDGET_MS = 60_000;
const sha256 = (value) => createHash("sha256").update(value).digest("hex");

function canonicalText(value) {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new Error("canonical JSON rejects non-finite numbers");
    return value.toExponential(16).replace(/e([+-])0*(\d+)$/, "e$1$2");
  }
  if (Array.isArray(value)) return `[${value.map(canonicalText).join(",")}]`;
  if (typeof value === "object") {
    const keys = Object.keys(value).sort(
      (left, right) => Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8")),
    );
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalText(value[key])}`).join(",")}}`;
  }
  throw new Error(`canonical JSON rejects ${typeof value}`);
}

export function canonicalJsonBytes(value) {
  return Buffer.from(canonicalText(value), "utf8");
}

export async function defaultSandboxFactory(opts) {
  const mod = await import("e2b"); // dynamic: hermetic callers never load the SDK
  const { template, ...createOpts } = opts;
  if (!template) throw new Error("E2B tool sandbox template is required");
  return mod.Sandbox.create(template, createOpts);
}

async function resolveApiKey() {
  if (process.env.E2B_API_KEY) return process.env.E2B_API_KEY;
  const file = process.env.E2B_API_KEY_FILE ?? DEFAULT_KEY_FILE;
  try {
    const key = (await readFile(file, "utf8")).trim();
    return key || null;
  } catch {
    return null;
  }
}

function b64Py(script) {
  const b64 = Buffer.from(script, "utf8").toString("base64");
  return `python3 -c "import base64; exec(base64.b64decode('${b64}'))"`;
}

/** The tiny fixed-size in-VM egress probe (safe to inline-b64, unlike the job). */
export function probeScript(
  brokerHost,
  deniedTargets,
  probeBroker,
  platformMetadataTarget = "http://169.254.169.254/latest/meta-data/",
) {
  return `
import json, os, subprocess, sys, urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urljoin
denied = json.loads(${JSON.stringify(JSON.stringify(deniedTargets))})
metadata_target = ${JSON.stringify(platformMetadataTarget)}
def check_blocked(target):
    try:
        with urllib.request.urlopen(target, timeout=5) as r:
            return target, {"blocked": False, "status": r.status}
    except HTTPError as exc:
        return target, {"blocked": False, "status": exc.code}
    except Exception as exc:
        return target, {"blocked": True, "error_type": type(exc).__name__}
with ThreadPoolExecutor(max_workers=min(8, len(denied))) as pool:
    blocked = dict(pool.map(check_blocked, denied))

def check_metadata(label, target, method="GET", headers=None):
    try:
        req = urllib.request.Request(target, headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=5) as r:
            return label, {"blocked": False, "status": r.status}
    except HTTPError as exc:
        return label, {"blocked": False, "status": exc.code}
    except Exception as exc:
        return label, {"blocked": True, "error_type": type(exc).__name__}

metadata_root = urljoin(metadata_target, "/")
metadata_attempt_specs = [
    ("no_auth_get", metadata_target, "GET", {}),
    ("aws_token_put", urljoin(metadata_root, "latest/api/token"), "PUT",
     {"X-aws-ec2-metadata-token-ttl-seconds": "21600"}),
    ("aws_invalid_token_get", metadata_target, "GET",
     {"X-aws-ec2-metadata-token": "leaf-invalid"}),
    ("gcp_flavor_get", metadata_target, "GET", {"Metadata-Flavor": "Google"}),
    ("e2b_invalid_access_token_get", metadata_target, "GET",
     {"X-Access-Token": "leaf-invalid"}),
    ("invalid_bearer_get", metadata_target, "GET",
     {"Authorization": "Bearer leaf-invalid"}),
]
with ThreadPoolExecutor(max_workers=len(metadata_attempt_specs)) as pool:
    metadata_attempts = dict(pool.map(lambda spec: check_metadata(*spec),
                                      metadata_attempt_specs))
known_credential_names = (
    "E2B_API_KEY", "E2B_ACCESS_TOKEN", "E2B_ENVD_ACCESS_TOKEN",
    "ENVD_ACCESS_TOKEN", "TRAFFIC_ACCESS_TOKEN", "X_ACCESS_TOKEN",
)
credential_material_present = any(os.environ.get(name) for name in known_credential_names)

namespace_script = r"""
import json, sys, urllib.request
from urllib.error import HTTPError
targets = json.loads(sys.argv[1])
blocked = {}
for target in targets:
    try:
        with urllib.request.urlopen(target, timeout=3) as response:
            blocked[target] = {"blocked": False, "status": response.status}
    except HTTPError as exc:
        blocked[target] = {"blocked": False, "status": exc.code}
    except Exception as exc:
        blocked[target] = {"blocked": True, "error_type": type(exc).__name__}
print(json.dumps(blocked))
"""
namespace_targets = list(dict.fromkeys([*denied, metadata_target]))
namespace_proc = subprocess.run(
    ["unshare", "-Urn", "--", sys.executable, "-c", namespace_script,
     json.dumps(namespace_targets)],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
)
try:
    namespace_blocked = json.loads(namespace_proc.stdout.strip())
except Exception:
    namespace_blocked = {}
tenant_network_namespace = {
    "command_ok": namespace_proc.returncode == 0,
    "blocked": namespace_blocked,
}
broker_ok = None
if ${probeBroker ? "True" : "False"}:
    try:
        req = urllib.request.Request(
            "https://" + ${JSON.stringify(brokerHost)} + "/post",
            data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            broker_ok = r.status == 200
    except Exception:
        broker_ok = False
print(json.dumps({
    "blocked": blocked,
    "platform_metadata": {
        "target": metadata_target,
        "credential_material_present": credential_material_present,
        "attempts": metadata_attempts,
    },
    "tenant_network_namespace": tenant_network_namespace,
    "broker_ok": broker_ok,
}))
`;
}

export async function runJob(envelope, factory = defaultSandboxFactory) {
  const out = { schema: SCHEMA_RESULT, receipt: null, result: null, helper_error: null };
  const fail = (stage, type, msg) => {
    out.helper_error = { stage, type: String(type), msg: String(msg) };
    return out;
  };

  const job = envelope?.job;
  const runnerPy = envelope?.runner_py;
  if (!job || typeof job !== "object" || typeof runnerPy !== "string" || !runnerPy) {
    return fail("parse", "BadJob", "job envelope missing job/runner_py");
  }
  const timeoutS = Number(envelope.timeout_s) > 0 ? Number(envelope.timeout_s) : 30;
  const brokerHost = (envelope.broker_host || "httpbingo.org").trim();
  const deniedTargets =
    Array.isArray(envelope.denied_targets) && envelope.denied_targets.length
      ? envelope.denied_targets
      : ["https://example.com/", "https://api.github.com/", "https://1.1.1.1/"];
  const probeBroker = envelope.probe_broker === true;
  const platformMetadataTarget = (
    envelope.platform_metadata_target ||
    "http://169.254.169.254/latest/meta-data/"
  ).trim();
  const audit = envelope.audit && typeof envelope.audit === "object" ? envelope.audit : null;
  const effectiveDeniedTargets = audit && !probeBroker
    ? [...new Set([...deniedTargets, `https://${brokerHost}/`])]
    : deniedTargets;
  const limits = audit?.limits ?? {};
  const outputLimit = Number(limits.output_bytes) > 0 ? Number(limits.output_bytes) : 1024 * 1024;
  const payloadBytes = Buffer.byteLength(JSON.stringify(job), "utf8");
  if (audit && payloadBytes >
      Number(limits.source_bytes ?? 0) + Number(limits.input_bytes ?? 0) +
      Number(limits.params_bytes ?? 0) + 4096) {
    return fail("limits", "PayloadTooLarge", "job payload exceeded fixed sandbox limits");
  }
  const allowOut = audit && !probeBroker ? [] : [brokerHost];

  const apiKey = await resolveApiKey();
  if (!apiKey) {
    return fail("key", "MissingKey",
      "no E2B API key (E2B_API_KEY / E2B_API_KEY_FILE / default key file)");
  }

  let sandbox = null;
  const bootStarted = performance.now();
  try {
    try {
      sandbox = await factory({
        apiKey,
        timeoutMs: Math.round(timeoutS * 1000 + PROBE_TIMEOUT_MS + BOOT_BUDGET_MS),
        secure: true,
        template: audit?.template_version ?? "base",
        metadata: {
          purpose: "leaf-tool-exec",
          policy: audit?.policy_version ?? "legacy",
        },
        network: {
          allowOut,
          denyOut: ["0.0.0.0/0"],
          allowPublicTraffic: false,
        },
      });
    } catch (e) {
      return fail("boot", e?.name ?? "BootError", e?.message ?? e);
    }
    const coldBootMs = Math.round(performance.now() - bootStarted);

    // Upload the job + runner. The intake never touches an argv (arg-limit safety).
    try {
      await sandbox.files.write(`${JOB_DIR}/job.json`, JSON.stringify(job));
      await sandbox.files.write(`${JOB_DIR}/runner.py`, runnerPy);
    } catch (e) {
      return fail("upload", e?.name ?? "UploadError", e?.message ?? e);
    }

    // Egress receipt: configured-policy cross-check + in-VM denied probes.
    let info;
    let probe;
    try {
      info = await sandbox.getInfo();
      const probeRes = await sandbox.commands.run(
        b64Py(probeScript(
          brokerHost,
          effectiveDeniedTargets,
          probeBroker,
          platformMetadataTarget,
        )),
        { timeoutMs: PROBE_TIMEOUT_MS });
      probe = JSON.parse((probeRes?.stdout ?? "").trim());
    } catch (e) {
      return fail("probe", e?.name ?? "ProbeError", e?.message ?? e);
    }
    const configuredDenyAll = info?.network?.denyOut?.includes("0.0.0.0/0") === true;
    const configuredTemplate =
      (!audit || (
        info?.templateId === audit?.template_id &&
        info?.name === audit?.template_version
      ));
    const configuredBrokerOnly = audit && !probeBroker
      ? (info?.network?.allowOut ?? []).length === 0
      : info?.network?.allowOut?.length === 1 && info.network.allowOut[0] === brokerHost;
    const configuredNoPublicTraffic = info?.network?.allowPublicTraffic === false;
    const everyDeniedProbeBlocked =
      effectiveDeniedTargets.length > 0 &&
      effectiveDeniedTargets.every((target) => probe?.blocked?.[target]?.blocked === true);
    const platformMetadata = probe?.platform_metadata ?? null;
    const tenantNetworkNamespace = probe?.tenant_network_namespace ?? null;
    const expectedNamespaceTargets =
      [...new Set([...effectiveDeniedTargets, platformMetadataTarget])];
    const tenantNetworkNamespaceIsolated =
      tenantNetworkNamespace?.command_ok === true &&
      Object.keys(tenantNetworkNamespace?.blocked ?? {}).length ===
        expectedNamespaceTargets.length &&
      expectedNamespaceTargets.every(
        (target) => tenantNetworkNamespace?.blocked?.[target]?.blocked === true,
      );
    const platformMetadataSafe =
      platformMetadata?.target === platformMetadataTarget &&
      platformMetadata?.credential_material_present === false &&
      tenantNetworkNamespaceIsolated;
    const brokerReached = probeBroker ? probe?.broker_ok === true : null;
    const passed =
      configuredTemplate && configuredDenyAll && configuredBrokerOnly && configuredNoPublicTraffic &&
      everyDeniedProbeBlocked && platformMetadataSafe &&
      (probeBroker ? brokerReached === true : true);

    out.receipt = {
      sandboxId: sandbox.sandboxId ?? null,
      coldBootMs,
      network: {
        allowOut: info?.network?.allowOut ?? null,
        allowOutReported: Array.isArray(info?.network?.allowOut),
        denyOut: info?.network?.denyOut ?? [],
        allowPublicTraffic: info?.network?.allowPublicTraffic ?? null,
      },
      configuredDenyAll,
      configuredTemplate,
      configuredBrokerOnly,
      configuredNoPublicTraffic,
      everyDeniedProbeBlocked,
      deniedProbes: probe?.blocked ?? {},
      tenantNetworkNamespace,
      tenantNetworkNamespaceIsolated,
      platformMetadata,
      platformMetadataSafe,
      brokerReached,
      boundary: "tool",
      tenantHash: audit?.tenant_hash ?? null,
      sourceHash: audit?.source_hash ?? null,
      inputHash: audit?.input_hash ?? null,
      jobHash: sha256(canonicalJsonBytes(job)),
      templateVersion: audit?.template_version ?? null,
      templateId: info?.templateId ?? null,
      templateBuildId: audit?.template_build_id ?? null,
      policyVersion: audit?.policy_version ?? null,
      startedAt: new Date(Date.now() - coldBootMs).toISOString(),
      passed,
    };

    // THE REFUSAL RULE: no tool output ever leaves an unproven sandbox.
    if (!passed) {
      return fail("probe", "EgressLockFailed",
        "sandbox egress lock could not be proven; tool output refused");
    }

    // Execute: the runner reads the job over stdin (redirect from the uploaded file),
    // exactly as tool_loader pipes it to the subprocess tier. NO `envs` option — the
    // API key (or anything else) can never leak into the VM environment this way.
    let execRes;
    try {
      execRes = await sandbox.commands.run(
        `python3 ${JOB_DIR}/runner.py < ${JOB_DIR}/job.json`,
        { timeoutMs: Math.round(timeoutS * 1000) });
    } catch (e) {
      return fail("exec", e?.name ?? "ExecError", e?.message ?? e);
    }
    const stdout = (execRes?.stdout ?? "").trim();
    if (Buffer.byteLength(stdout, "utf8") > outputLimit) {
      return fail("limits", "OutputTooLarge", "runner output exceeded fixed sandbox limit");
    }
    if (!stdout) {
      return fail("exec", "NoOutput",
        `runner produced no output (exit=${execRes?.exitCode})`);
    }
    try {
      out.result = JSON.parse(stdout); // relay the runner's JSON VERBATIM
      if (audit) {
        const stoppedAt = new Date().toISOString();
        out.receipt.stoppedAt = stoppedAt;
        out.receipt.stopReason = "completed";
        out.receipt.resourceUse = {
          wallMs: Math.max(0, Date.parse(stoppedAt) - Date.parse(out.receipt.startedAt)),
          inputBytes: payloadBytes,
          outputBytes: Buffer.byteLength(stdout, "utf8"),
          limits,
          cpuSeconds: null,
          peakMemoryBytes: null,
          processes: null,
          diskBytes: null,
          files: null,
        };
        out.receipt.resultHash = sha256(canonicalJsonBytes(out.result));
      }
    } catch (e) {
      return fail("parse", "BadRunnerOutput",
        `runner stdout was not JSON: ${e?.message ?? e}`);
    }
    return out;
  } finally {
    if (sandbox) {
      await sandbox.kill().catch(() => false);
      if (out.receipt) out.receipt.teardownAt = new Date().toISOString();
    }
  }
}

// ---------------------------------------------------------------------------
// CLI: stdin job blob -> stdout result blob. Exit 0 iff the blob was emitted —
// the broker maps helper_error/receipt failures to its own infra_error envelope.
// ---------------------------------------------------------------------------
const isMain =
  process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (isMain) {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  let out;
  try {
    const envelope = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    out = await runJob(envelope);
  } catch (e) {
    out = {
      schema: SCHEMA_RESULT,
      receipt: null,
      result: null,
      helper_error: { stage: "parse", type: "BadStdin", msg: String(e?.message ?? e) },
    };
  }
  process.stdout.write(`${JSON.stringify(out)}\n`);
}
