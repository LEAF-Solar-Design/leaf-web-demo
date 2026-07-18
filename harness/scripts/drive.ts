/**
 * LIVE DRIVE — the milestone script (operator-gated; NOT part of the hermetic gate).
 *
 * Wires the four REAL ports, boots the real HTTP harness in-process, then:
 *   1. POST /author  -> a REAL Agent SDK session authors a deterministic CAD tool
 *      for a prompt with no built-in family, validates it, registers it, and makes
 *      exactly one harness git commit in the tenant repo.
 *   2. POST /run-registered -> runs the registered tool through the REAL broker
 *      (mock path, aps_live=false) against the real 2345-polyline intake, with ZERO
 *      LLM (proven: the AgentRunner is not invoked on the run path).
 *   3. Independently recompute the expected bounding boxes and compare.
 *   4. Write data/nl_author_receipt.json.
 *
 * Secret discipline: the grant is resolved by EnvOrFileGrantStore and injected into
 * a scrubbed SDK child env inside AgentSdkRunner. This driver never reads, prints,
 * or logs the token; any KEY/TOKEN/SECRET-shaped value is redacted from stderr.
 */

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";

import { createHarness } from "../src/server.js";
import type { AgentRunInput, AgentRunResult, HarnessPorts } from "../src/ports/index.js";
import { validateToolPackage } from "../src/registry/toolPackageSchema.js";
import { AgentSdkRunner } from "../src/ports/impl/agentSdkRunner.js";
import { BrokerApsClientHttp } from "../src/ports/impl/brokerApsClient.js";
import { OAuthGrantProviderImpl, EnvOrFileGrantStore } from "../src/ports/impl/oauthGrantProvider.js";
import { TenantRepoProviderImpl } from "../src/ports/impl/tenantRepoProvider.js";

const REPO_ROOT = process.env.LEAF_REPO_ROOT ?? "C:/tmp/leaf-web-demo";
const TENANT_DIR = process.env.LEAF_TENANT_DIR ?? "C:/tmp/leaf-tenants/demo-tenant";
const BROKER_URL = process.env.BROKER_URL ?? "http://127.0.0.1:8140";
const INTAKE_PATH = `${REPO_ROOT}/data/rooftop_demo.intake.json`;
const RECEIPT_PATH = `${REPO_ROOT}/data/nl_author_receipt.json`;
const TENANT = "demo-tenant";
const PROMPT =
  process.env.LEAF_PROMPT ??
  "Report the bounding box (min/max x,y) of each layer's geometry, sorted by area (largest first).";

const TOKENISH = /\b(sk-ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{40,})\b/g;
function redact(s: string): string {
  return s.replace(TOKENISH, "[REDACTED]");
}
function log(...parts: unknown[]): void {
  process.stderr.write(redact(parts.map(String).join(" ")) + "\n");
}

// ---- independent bounding-box recompute (the oracle we compare against) ----
interface Box { min_x: number; min_y: number; max_x: number; max_y: number; area: number; count: number }
function independentBoxes(intake: Record<string, unknown>): Record<string, Box> {
  const out: Record<string, Box> = {};
  const polylines = (intake.polylines as Array<Record<string, unknown>>) ?? [];
  for (const pl of polylines) {
    const layer = typeof pl.layer === "string" ? pl.layer : "0";
    const pts = (pl.pts as number[][]) ?? [];
    let b = out[layer];
    if (!b) {
      b = { min_x: Infinity, min_y: Infinity, max_x: -Infinity, max_y: -Infinity, area: 0, count: 0 };
      out[layer] = b;
    }
    for (const pt of pts) {
      const x = pt[0];
      const y = pt[1];
      if (x < b.min_x) b.min_x = x;
      if (y < b.min_y) b.min_y = y;
      if (x > b.max_x) b.max_x = x;
      if (y > b.max_y) b.max_y = y;
      b.count += 1;
    }
  }
  for (const b of Object.values(out)) {
    b.area = (b.max_x - b.min_x) * (b.max_y - b.min_y);
    b.min_x = round3(b.min_x);
    b.min_y = round3(b.min_y);
    b.max_x = round3(b.max_x);
    b.max_y = round3(b.max_y);
    b.area = round3(b.area);
  }
  return out;
}
function round3(n: number): number {
  return Math.round(n * 1000) / 1000;
}

/** Flatten every finite number found anywhere in a JSON value. */
function collectNumbers(v: unknown, acc: number[]): void {
  if (typeof v === "number" && Number.isFinite(v)) acc.push(v);
  else if (Array.isArray(v)) for (const x of v) collectNumbers(x, acc);
  else if (v && typeof v === "object") for (const x of Object.values(v)) collectNumbers(x, acc);
}
/** True iff `target` appears among `nums` within a relative/absolute tolerance. */
function present(nums: number[], target: number): boolean {
  const tol = Math.max(0.05, Math.abs(target) * 1e-4);
  return nums.some((n) => Math.abs(n - target) <= tol);
}

async function main(): Promise<void> {
  const sdkVersion = JSON.parse(
    readFileSync(`${REPO_ROOT}/harness/node_modules/@anthropic-ai/claude-agent-sdk/package.json`, "utf8"),
  ).version as string;
  log(`=== LIVE DRIVE ===  sdk=${sdkVersion}  broker=${BROKER_URL}  tenant_dir=${TENANT_DIR}`);
  log(`PROMPT: ${PROMPT}`);

  // ---- real ports ----
  const runner = new AgentSdkRunner({ maxTurns: 40, maxTotalTokens: 500_000 });
  let runnerCalls = 0;
  const origRun = runner.run.bind(runner);
  // wrap run() to PROVE the run path never constructs the SDK session
  (runner as { run: (i: AgentRunInput) => Promise<AgentRunResult> }).run = async (i: AgentRunInput) => {
    runnerCalls += 1;
    return origRun(i);
  };

  const ports: HarnessPorts = {
    oauth: new OAuthGrantProviderImpl({ store: new EnvOrFileGrantStore() }),
    tenantRepo: new TenantRepoProviderImpl({ locator: { async repoRef() { return TENANT_DIR; } }, inPlace: true }),
    broker: new BrokerApsClientHttp({ brokerUrl: BROKER_URL }),
    agentRunner: runner,
  };

  const server: Server = createHarness(ports).listen(0);
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  const receipt: Record<string, unknown> = {
    pass: false,
    prompt: PROMPT,
    sdk_package_version: sdkVersion,
    broker_url: BROKER_URL,
    tenant_repo: TENANT_DIR,
    ran_at: new Date().toISOString(),
  };

  try {
    // health
    const health = await fetch(`${base}/health`).then((r) => r.json());
    log(`health: ${JSON.stringify(health)}`);

    // 1) AUTHOR (real SDK)
    log("POST /author  (real Agent SDK session authoring...)");
    const t0 = Date.now();
    const authorRes = await fetch(`${base}/author`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": TENANT },
      body: JSON.stringify({ description: PROMPT }),
    });
    const authorBody = (await authorRes.json()) as {
      tool: { name: string; engine_op: string; entry?: string; version: string };
      code: string;
      preview: string;
    };
    const authorMs = Date.now() - t0;
    log(`/author -> HTTP ${authorRes.status} in ${authorMs} ms; runnerCalls=${runnerCalls}`);
    if (authorRes.status !== 200) {
      receipt.error = `author failed: HTTP ${authorRes.status} ${JSON.stringify(authorBody)}`;
      throw new Error(receipt.error as string);
    }
    const tool = authorBody.tool as { name: string; engine_op: string; entry?: string; version: string };
    const code = authorBody.code as string;
    log(`authored tool: ${tool.name}  engine_op=${tool.engine_op}  entry=${tool.entry}`);

    const usage = runner.lastRun;
    receipt.tool_name = tool.name;
    receipt.engine_op = tool.engine_op;
    receipt.tool = tool;
    receipt.preview = authorBody.preview;
    receipt.turns = usage?.turns ?? null;
    receipt.usage_totals = usage?.usage_totals ?? null;
    receipt.per_turn_usage = usage?.per_turn ?? [];
    receipt.total_cost_usd = usage?.total_cost_usd ?? null;
    receipt.models = usage?.models ?? [];
    receipt.session_id = usage?.session_id ?? null;
    receipt.author_ms = authorMs;
    receipt.authored_files = tool.entry ? [tool.entry, `tools/${tool.name}/tool.json`] : [];
    receipt.entry_code = code;
    receipt.validation_result = { ok: validateToolPackage(authorBody.tool).length === 0, diagnostics: validateToolPackage(authorBody.tool) };

    // tenant repo commit (build makes exactly one harness commit)
    const commit = execFileSync("git", ["-C", TENANT_DIR, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
    const subject = execFileSync("git", ["-C", TENANT_DIR, "log", "-1", "--format=%an|%s"], { encoding: "utf8" }).trim();
    receipt.tenant_repo_commit = commit;
    receipt.tenant_repo_commit_subject = subject;
    log(`tenant commit: ${commit.slice(0, 12)}  (${subject})`);
    log(`usage: turns=${usage?.turns} totals=${JSON.stringify(usage?.usage_totals)} cost=${usage?.total_cost_usd} models=${JSON.stringify(usage?.models)}`);

    // 2) RUN-REGISTERED (zero LLM, real broker, real intake)
    const runnerCallsBefore = runnerCalls;
    log("POST /run-registered  (zero-LLM execution via the real broker)...");
    const runRes = await fetch(`${base}/run-registered`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-tenant-id": TENANT },
      body: JSON.stringify({ tool: tool.name, params: {}, dwg: "rooftop_demo", aps_live: false }),
    });
    const envelope = (await runRes.json()) as {
      ok: boolean;
      tool: string;
      version: string;
      result: Record<string, unknown>;
      timing_ms: number;
      degraded_mode?: boolean;
      error: unknown;
    };
    const zeroLlm = runnerCalls === runnerCallsBefore;
    log(`/run-registered -> HTTP ${runRes.status}  ok=${envelope.ok}  AgentRunner NOT invoked on run path: ${zeroLlm}`);
    receipt.run_registered_result_summary = {
      http_status: runRes.status,
      ok: envelope.ok,
      tool: envelope.tool,
      version: envelope.version,
      timing_ms: envelope.timing_ms,
      degraded_mode: envelope.degraded_mode,
      error: envelope.error,
      result: envelope.result,
    };
    receipt.zero_llm_on_run_path = zeroLlm;

    // 3) INDEPENDENT CHECK
    const intake = JSON.parse(readFileSync(INTAKE_PATH, "utf8")) as Record<string, unknown>;
    const expected = independentBoxes(intake);
    const nums: number[] = [];
    collectNumbers(envelope.result, nums);
    // Every layer that HAS geometry must have its 4 extents + area present in the result.
    const perLayer: Record<string, { expected: Box; matched: boolean }> = {};
    let allMatch = true;
    for (const [layer, b] of Object.entries(expected)) {
      if (b.count === 0) continue;
      const matched =
        present(nums, b.min_x) && present(nums, b.min_y) &&
        present(nums, b.max_x) && present(nums, b.max_y) && present(nums, b.area);
      perLayer[layer] = { expected: b, matched };
      if (!matched) allMatch = false;
    }
    receipt.independent_check = {
      expected,
      got: envelope.result,
      per_layer_match: perLayer,
      match: allMatch,
    };
    log(`independent check: match=${allMatch}  expected(Panels)=${JSON.stringify(expected["Panels"])}`);

    const pass =
      authorRes.status === 200 &&
      (receipt.validation_result as { ok: boolean }).ok &&
      runRes.status === 200 &&
      envelope.ok === true &&
      zeroLlm &&
      allMatch &&
      (usage?.turns ?? 99) <= 40 &&
      (usage?.usage_totals.total_tokens ?? 1e9) <= 500_000 &&
      /^[0-9a-f]{40}$/.test(commit);
    receipt.pass = pass;
    log(`VERDICT: ${pass ? "PASS" : "FAIL"}`);
  } catch (e) {
    receipt.error = redact((e as Error).message);
    log(`ERROR: ${redact((e as Error).message)}`);
    if ((e as Error).stack) log(redact((e as Error).stack as string));
    // capture whatever usage was metered before the failure (transparency)
    const u = runner.lastRun;
    if (u && receipt.turns == null) {
      receipt.turns = u.turns;
      receipt.usage_totals = u.usage_totals;
      receipt.per_turn_usage = u.per_turn;
      receipt.total_cost_usd = u.total_cost_usd;
      receipt.models = u.models;
      receipt.session_id = u.session_id;
    }
  } finally {
    writeFileSync(RECEIPT_PATH, JSON.stringify(receipt, null, 2) + "\n", "utf8");
    log(`receipt written: ${RECEIPT_PATH}`);
    server.close();
  }

  process.exit(receipt.pass ? 0 : 1);
}

main().catch((e) => {
  log(`FATAL: ${redact((e as Error).message)}`);
  process.exit(2);
});
