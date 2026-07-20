/**
 * A1 authoring-telemetry acceptance (hermetic; NO network, NO real Anthropic creds).
 *
 * Proves the additive /author telemetry contract end-to-end through the FULL pipeline
 * (server -> AuthorLoop -> AgentRunner port), wired to the same fakes + a REAL git
 * working copy of test/fixtures/tenant-repo the other e2e tests use:
 *
 *   1. When the runner METERS a build (injects telemetry), POST /author surfaces the
 *      exact field shape { turns, input_tokens, output_tokens, total_cost_usd, models }
 *      alongside {tool, code, preview}.
 *   2. Telemetry is OPTIONAL / ABSENT-SAFE: a non-metering runner (the stock fake)
 *      yields a response whose keys are EXACTLY {tool, code, preview} — the frozen shape.
 *   3. Per-field optionality: a runner that meters only some dimensions surfaces only
 *      those keys (no fabricated total_cost_usd / models) — what the web chip relies on.
 *
 * The telemetry-injecting runner DELEGATES to the stock FakeAgentRunner so the real
 * build steps (write tool.py + tool.json, validate against CONTRACT section 2, register,
 * commit) still run — telemetry is layered onto the genuine authoring result.
 */

import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type {
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  AuthorTelemetry,
  HarnessPorts,
} from "../src/ports/index.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

/**
 * A metering runner: delegates authoring to the stock fake (so the build pipeline runs
 * for real), then layers the given telemetry onto the result — modelling the real
 * AgentSdkRunner, which populates AgentRunResult.telemetry from its self-metered run.
 * `telemetry === null` models a non-metering runner (leaves the field undefined).
 */
class MeteringFakeRunner implements AgentRunner {
  private readonly inner = new FakeAgentRunner();
  constructor(private readonly telemetry: AuthorTelemetry | null) {}
  async run(input: AgentRunInput): Promise<AgentRunResult> {
    const result = await this.inner.run(input);
    return this.telemetry ? { ...result, telemetry: this.telemetry } : result;
  }
}

describe("POST /author telemetry (A1) - hermetic", () => {
  const servers: Server[] = [];

  afterEach(() => {
    for (const s of servers.splice(0)) s.close();
  });

  function harnessWith(runner: AgentRunner): string {
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: runner,
    };
    const server = createHarness(ports).listen(0);
    servers.push(server);
    const addr = server.address() as AddressInfo;
    return `http://127.0.0.1:${addr.port}`;
  }

  async function author(baseUrl: string): Promise<{ status: number; body: Record<string, unknown> }> {
    const res = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    return { status: res.status, body: (await res.json()) as Record<string, unknown> };
  }

  it("surfaces the runner's full telemetry on the /author build response", async () => {
    const telemetry: AuthorTelemetry = {
      turns: 3,
      input_tokens: 1200,
      output_tokens: 340,
      total_cost_usd: 0.0123,
      models: ["claude-opus-4-8"],
    };
    const baseUrl = harnessWith(new MeteringFakeRunner(telemetry));
    const { status, body } = await author(baseUrl);

    expect(status).toBe(200);
    // Additive: the frozen fields are still present and correct.
    expect(typeof body.tool).toBe("object");
    expect(typeof body.code).toBe("string");
    expect(typeof body.preview).toBe("string");
    // And telemetry is surfaced verbatim, exact A1 field shape.
    expect(body.telemetry).toEqual(telemetry);
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "telemetry", "tool"]);
  });

  it("is OPTIONAL / absent-safe: a non-metering runner keeps the frozen {tool,code,preview} shape", async () => {
    const baseUrl = harnessWith(new MeteringFakeRunner(null)); // no telemetry layered on
    const { status, body } = await author(baseUrl);

    expect(status).toBe(200);
    expect(body.telemetry).toBeUndefined();
    expect(Object.keys(body).sort()).toEqual(["code", "preview", "tool"]);
  });

  it("surfaces only the metered dimensions (no fabricated cost / models)", async () => {
    // A runner that could not measure cost or model surfaces just turns + tokens.
    const partial: AuthorTelemetry = { turns: 2, input_tokens: 500, output_tokens: 100 };
    const baseUrl = harnessWith(new MeteringFakeRunner(partial));
    const { status, body } = await author(baseUrl);

    expect(status).toBe(200);
    const t = body.telemetry as Record<string, unknown>;
    expect(t).toEqual(partial);
    // No absent field was invented.
    expect(Object.keys(t).sort()).toEqual(["input_tokens", "output_tokens", "turns"]);
    expect("total_cost_usd" in t).toBe(false);
    expect("models" in t).toBe(false);
  });
});
