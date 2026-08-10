/**
 * REAL server-catalog-to-harness regression (PR #541's follow-up requirement
 * for #542): proves that when the trusted operator-owned engine winner
 * carries live APS and runtime APS is enabled, the REAL server catalog
 * (server/deps.py's effective_tools_with_provenance — the PRODUCTION seam,
 * never monkeypatched — + server/catalog.py + server/routers/capabilities.py,
 * computed by a real Python subprocess over real stores on disk — see
 * test/fixtures/live-aps-catalog-fixture.py) drives the REAL harness
 * (HttpAppRunClient, never FakeAppRunClient/FAKE_CATALOG) to select
 * `submit_live_solve` for `count-by-layer` inside the REAL ConverseLoop
 * run_capability gate-consult logic (converseLoop.ts).
 *
 * Every prior converseLoop.test.ts coverage of this rung used a hand-typed
 * fake catalog entry (FAKE_CATALOG's "solve-live") that never touched the
 * Python catalog code at all, so a break in catalog.py's selection algebra,
 * the router's provenance wiring, or a field-name drift between the server's
 * JSON and the harness's CapabilityEntry parsing would go undetected. This
 * file closes that gap, plus proves the four fail-closed cases through the
 * SAME real path.
 */
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { ConverseLoop } from "../src/agent/converseLoop.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";

const PACKET = {
  catalog: [{ name: "count-by-layer", description: "Count entities per layer", capabilities: ["drawing.read"] }],
  drawing: { id: "rooftop_demo", head_version: 3 },
  grant: { kind: "oauth", degraded: false },
};

type Scenario =
  | "engine_winner_live"
  | "runtime_off"
  | "malformed_registry"
  | "shadow"
  | "non_boolean_marker";

/** Run the REAL Python catalog computation (server/catalog.py + capabilities.py,
 * unmocked) for one scenario and return its exact /api/capabilities JSON body. */
function realServerCatalogBody(scenario: Scenario): Record<string, unknown> {
  const repository = join(process.cwd(), "..");
  const venvPython = join(
    repository,
    ".venv",
    process.platform === "win32" ? "Scripts" : "bin",
    process.platform === "win32" ? "python.exe" : "python",
  );
  const python = process.env.PYTHON ?? (existsSync(venvPython) ? venvPython : "python");
  const stdout = execFileSync(
    python,
    ["-u", join(process.cwd(), "test", "fixtures", "live-aps-catalog-fixture.py"), scenario],
    { cwd: repository, encoding: "utf8" },
  );
  return JSON.parse(stdout) as Record<string, unknown>;
}

/** A real HttpAppRunClient whose fetchImpl answers from the real server-computed
 * catalog body (never a hand-typed fixture) and a canned drawing-versions row. */
function realAppRunClientFor(catalogBody: Record<string, unknown>): HttpAppRunClient {
  return new HttpAppRunClient({
    baseUrl: "https://app.invalid",
    dispatchSecret: "test-secret",
    fetchImpl: async (input) => {
      const url = String(input);
      if (url.endsWith("/api/capabilities")) {
        return new Response(JSON.stringify(catalogBody), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }
      if (url.includes("/api/drawings/") && url.endsWith("/versions")) {
        return new Response(
          JSON.stringify({
            drawing_id: "rooftop_demo",
            head: 3,
            latest: 3,
            versions: [],
            checkout: null,
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      if (url.endsWith("/api/run")) {
        return new Response(JSON.stringify({ job_id: "job-1", status: "submitted" }), {
          status: 202,
          headers: { "content-type": "application/json" },
        });
      }
      throw new Error(`unexpected fetch in serverCatalogLiveAps.test.ts: ${url}`);
    },
  });
}

function makeRealLoop(scenario: Scenario) {
  const catalogBody = realServerCatalogBody(scenario);
  const appRun = realAppRunClientFor(catalogBody);
  const gate = new FakeGateClient();
  const store = new FakeSessionStore();
  const runner = new FakeConverseRunner();
  const loop = new ConverseLoop({ runner, appRun, gate, store }, { model: "claude-sonnet-5" });
  return { loop, gate, store, catalogBody };
}

describe("server-catalog-to-harness — real live-APS regression (R4, count-by-layer)", () => {
  it("selects submit_live_solve for count-by-layer when the REAL server catalog reports a trusted operator-owned engine winner with runtime APS enabled", async () => {
    const { loop, gate, store } = makeRealLoop("engine_winner_live");
    const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

    const { turnId, done } = await loop.handleMessage({
      sessionId: s.session_id,
      tenantId: "demo-tenant",
      text: "RUN:count-by-layer",
      contextPacket: PACKET,
    });
    await done;
    expect(turnId).toBeTruthy();

    expect(gate.checks).toHaveLength(1);
    expect(gate.checks[0]!.action).toBe("submit_live_solve");
    expect(gate.checks[0]!.args).toMatchObject({ tool: "count-by-layer" });
    // The R4 rung always splits into an approval turn first — nothing dispatched yet.
    const events = await store.eventsAfter(s.session_id, 0);
    expect(events.some((e) => e.type === "proposed_run")).toBe(true);
  });

  it.each<[Scenario, string]>([
    ["runtime_off", "runtime APS disabled"],
    ["malformed_registry", "malformed engine registry (real fallback path)"],
    ["shadow", "same-name tenant-repo shadow of the engine tool"],
    ["non_boolean_marker", "non-boolean aps_live marker"],
  ])(
    "fails closed (never submit_live_solve) for count-by-layer when %s: %s",
    async (scenario) => {
      const { loop, gate } = makeRealLoop(scenario);
      const s = await loop.createOrGetSession("demo-tenant", "rooftop_demo");

      const { done } = await loop.handleMessage({
        sessionId: s.session_id,
        tenantId: "demo-tenant",
        text: "RUN:count-by-layer",
        contextPacket: PACKET,
      });
      await done;

      expect(gate.checks).toHaveLength(1);
      expect(gate.checks[0]!.action).toBe("run_read_tool");
      expect(gate.checks[0]!.action).not.toBe("submit_live_solve");
    },
  );
});
