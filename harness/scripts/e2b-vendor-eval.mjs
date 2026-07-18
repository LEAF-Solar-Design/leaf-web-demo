import { readFile, writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";
import { Sandbox } from "e2b";

const apiKeyPath = process.env.E2B_API_KEY_FILE ?? "C:\\tmp\\leaf-grants\\e2b-api-key.txt";
const receiptPath = process.env.E2B_RECEIPT_PATH ?? "..\\docs\\e2b-vendor-eval-receipt.json";
const brokerHost = "httpbingo.org";
const brokerUrl = `https://${brokerHost}/post`;
const deniedTargets = [
  "https://example.com/",
  "https://api.github.com/",
  "https://1.1.1.1/",
];

const apiKey = (await readFile(apiKeyPath, "utf8")).trim();
if (!apiKey) throw new Error(`E2B API key file is empty: ${apiKeyPath}`);

const startedAt = new Date().toISOString();
const bootStarted = performance.now();
let sandbox;

try {
  sandbox = await Sandbox.create({
    apiKey,
    timeoutMs: 120_000,
    secure: true,
    metadata: { purpose: "leaf-vendor-eval" },
    network: {
      allowOut: [brokerHost],
      denyOut: ["0.0.0.0/0"],
      allowPublicTraffic: false,
    },
  });

  const coldBootMs = Math.round(performance.now() - bootStarted);
  const info = await sandbox.getInfo();
  const probeScript = `
import json, urllib.request, urllib.error

broker_url = ${JSON.stringify(brokerUrl)}
denied_targets = ${JSON.stringify(deniedTargets)}
authoring_request = {
    "event": "authoring_process_booted",
    "tool": "draft_panel_layout",
    "tenant": "vendor-eval",
    "capabilities": ["read_intake", "emit_tool_package"],
}

payload = json.dumps(authoring_request).encode("utf-8")
request = urllib.request.Request(
    broker_url,
    data=payload,
    headers={"Content-Type": "application/json", "User-Agent": "leaf-e2b-vendor-eval/1"},
    method="POST",
)

with urllib.request.urlopen(request, timeout=10) as response:
    broker_body = json.loads(response.read().decode("utf-8"))

broker_ok = (
    broker_body.get("url", "").endswith("/post")
    and json.loads(broker_body.get("data", "{}")) == authoring_request
)

blocked = {}
for target in denied_targets:
    try:
        with urllib.request.urlopen(target, timeout=5) as response:
            blocked[target] = {"blocked": False, "status": response.status}
    except Exception as exc:
        blocked[target] = {"blocked": True, "error_type": type(exc).__name__}

print(json.dumps({
    "process_shape": authoring_request,
    "broker_ok": broker_ok,
    "blocked": blocked,
}, sort_keys=True))
`;

  const encodedProbe = Buffer.from(probeScript, "utf8").toString("base64");
  const command = `python3 -c "import base64; exec(base64.b64decode('${encodedProbe}'))"`;
  const result = await sandbox.commands.run(command, { timeoutMs: 45_000 });
  const probe = JSON.parse(result.stdout.trim());

  const configuredDenyAll = info.network?.denyOut?.includes("0.0.0.0/0") === true;
  const configuredBrokerOnly =
    info.network?.allowOut?.length === 1 && info.network.allowOut[0] === brokerHost;
  const everyDeniedProbeBlocked = Object.values(probe.blocked).every((entry) => entry.blocked);
  const passed =
    result.exitCode === 0 &&
    probe.broker_ok === true &&
    everyDeniedProbeBlocked &&
    configuredDenyAll &&
    configuredBrokerOnly;

  const receipt = {
    schema: "leaf.e2b.vendor-eval.v1",
    startedAt,
    completedAt: new Date().toISOString(),
    sdkVersion: "2.35.0",
    sandboxId: sandbox.sandboxId,
    coldBootMs,
    network: {
      allowOut: info.network?.allowOut ?? [],
      denyOut: info.network?.denyOut ?? [],
      allowPublicTraffic: info.network?.allowPublicTraffic,
    },
    processShape: probe.process_shape,
    broker: { host: brokerHost, reached: probe.broker_ok },
    deniedProbes: probe.blocked,
    commandExitCode: result.exitCode,
    passed,
  };

  await writeFile(receiptPath, `${JSON.stringify(receipt, null, 2)}\n`, "utf8");
  console.log(JSON.stringify(receipt, null, 2));
  if (!passed) process.exitCode = 1;
} finally {
  if (sandbox) await sandbox.kill().catch(() => false);
}
