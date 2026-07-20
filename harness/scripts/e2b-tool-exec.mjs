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

const SCHEMA_RESULT = "leaf.e2b.tool-exec-result.v1";
const DEFAULT_KEY_FILE = "C:\\tmp\\leaf-grants\\e2b-api-key.txt";
const JOB_DIR = "/home/user/leaf-job";

export async function defaultSandboxFactory(opts) {
  const mod = await import("e2b"); // dynamic: hermetic callers never load the SDK
  return mod.Sandbox.create(opts);
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
function probeScript(brokerHost, deniedTargets, probeBroker) {
  return `
import json, urllib.request
denied = json.loads(${JSON.stringify(JSON.stringify(deniedTargets))})
blocked = {}
for target in denied:
    try:
        with urllib.request.urlopen(target, timeout=5) as r:
            blocked[target] = {"blocked": False, "status": r.status}
    except Exception as exc:
        blocked[target] = {"blocked": True, "error_type": type(exc).__name__}
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
print(json.dumps({"blocked": blocked, "broker_ok": broker_ok}))
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
        timeoutMs: Math.round((timeoutS + 60) * 1000),
        secure: true,
        metadata: { purpose: "leaf-tool-exec" },
        network: {
          allowOut: [brokerHost],
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
        b64Py(probeScript(brokerHost, deniedTargets, probeBroker)),
        { timeoutMs: 30_000 });
      probe = JSON.parse((probeRes?.stdout ?? "").trim());
    } catch (e) {
      return fail("probe", e?.name ?? "ProbeError", e?.message ?? e);
    }
    const blockedEntries = Object.values(probe?.blocked ?? {});
    const configuredDenyAll = info?.network?.denyOut?.includes("0.0.0.0/0") === true;
    const configuredBrokerOnly =
      info?.network?.allowOut?.length === 1 && info.network.allowOut[0] === brokerHost;
    const everyDeniedProbeBlocked =
      blockedEntries.length > 0 && blockedEntries.every((entry) => entry.blocked === true);
    const brokerReached = probeBroker ? probe?.broker_ok === true : null;
    const passed =
      configuredDenyAll && configuredBrokerOnly && everyDeniedProbeBlocked &&
      (probeBroker ? brokerReached === true : true);

    out.receipt = {
      sandboxId: sandbox.sandboxId ?? null,
      coldBootMs,
      network: {
        allowOut: info?.network?.allowOut ?? [],
        denyOut: info?.network?.denyOut ?? [],
        allowPublicTraffic: info?.network?.allowPublicTraffic ?? null,
      },
      configuredDenyAll,
      configuredBrokerOnly,
      everyDeniedProbeBlocked,
      brokerReached,
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
    if (!stdout) {
      return fail("exec", "NoOutput",
        `runner produced no output (exit=${execRes?.exitCode})`);
    }
    try {
      out.result = JSON.parse(stdout); // relay the runner's JSON VERBATIM
    } catch (e) {
      return fail("parse", "BadRunnerOutput",
        `runner stdout was not JSON: ${e?.message ?? e}`);
    }
    return out;
  } finally {
    if (sandbox) await sandbox.kill().catch(() => false);
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
