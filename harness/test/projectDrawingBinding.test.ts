import { expect, it } from "vitest";
import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { FAKE_CATALOG } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { ConverseRunInput, ConverseRunnerEvent } from "../src/ports/index.js";
const drawing = "33333333-3333-4333-8333-333333333333";
const version = "44444444-4444-4444-8444-444444444444";
const state = { binding: { schema: "leaf.project-execution-context.v1",
  organization_id: "11111111-1111-4111-8111-111111111111",
  project_id: "22222222-2222-4222-8222-222222222222", drawing_id: drawing,
  input_version_id: version, intake_sha256: "a".repeat(64) },
  revision: 3, graph_revision: 17, head_version_id: version, is_head: true,
  summary: { entity_total: 12, layers: [{ name: "Panels", count: 12 }] } };
async function setup(reply: unknown = state, status = 200, solveArgs?: Record<string, unknown>) {
  const requests: Array<{ path: string; body: any }> = [];
  const client = new HttpAppRunClient({ baseUrl: "http://app", dispatchSecret: "synthetic",
    fetchImpl: (async (url, init) => {
      const path = new URL(String(url)).pathname;
      requests.push({ path, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (path === "/internal/integrations/project-drawing-state") return new Response(JSON.stringify(reply), { status });
      if (path === "/api/capabilities") return Response.json({ families: [{ capabilities:
        [...FAKE_CATALOG, { ...FAKE_CATALOG.find(c => c.name === "add-panel"), name: "solar-settings" },
          { ...FAKE_CATALOG.find(c => c.name === "add-panel"), name: "solar-solve-proposal",
            capabilities: ["solve"] }] }] });
      if (path === "/api/run") return Response.json({ job_id: "solve-job", status: "queued" }, { status: 202 });
      if (path === "/api/drawings/rooftop_demo/summary") return Response.json({ drawing_id: "rooftop_demo", head: 1 });
      throw new Error("Unexpected legacy or dispatch request: " + path);
    }) as typeof fetch });
  const gate = new FakeGateClient();
  const store = new FakeSessionStore();
  await store.createOrGetSession("tenant", "rooftop_demo");
  const scripted = new FakeConverseRunner();
  const solveRunner = {
    async *run(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent> {
      if (input.userMessage.includes("=== CONFIRMATION ===")) { yield* scripted.run(input); return; }
      const verdict = await input.canUseTool("mcp__spine__run_capability", solveArgs!);
      if (verdict.behavior === "allow") {
        const result = await input.tools.execute("run_capability", verdict.updatedInput);
        yield { type: "text_delta", text: result.content };
      }
      yield { type: "done", stopReason: "end_turn", sdkSessionId: "solve-test" };
    },
  };
  const adapter = new SpineTurnAdapter({ appRun: client, gate, store,
    oauth: new FakeOAuthGrantProvider(), runnerFor: () => solveArgs ? solveRunner : scripted });
  const run = async (text: string) => {
    const events = [];
    for await (const event of adapter.runTurn({ tenant_id: "tenant", session_id: "app-session",
      turn_id: "app-turn", drawing_id: "rooftop_demo", messages: [], text })) events.push(event);
    return events;
  };
  return { requests, gate, run, client, adapter };
}

it("binds canonical solve approval and the actual HTTP dispatch to the same context", async () => {
  const context = { frame_ref: "roof-frame", expected_rev: 17, phase: "initial" };
  const params = { grant_ref: "fixture-grant", request: { num_mppt: 2 } };
  const { run, requests, gate, adapter } = await setup(state, 200,
    { tool: "solar-solve-proposal", params, solve_context: context });
  const events = await run("SOLVE");
  const proposed = events.find(event => event.type === "proposed_run")!;
  expect(proposed.data.capability).toBe("solve");
  const check = gate.checks.find(call => call.action === "run_solve_tool")!;
  expect(check.args).toMatchObject({ dwg: drawing, drawing_version: 3,
    expected_drawing_head: 3, project_execution_context: state.binding,
    solve_context: context, params });
  expect(requests.some(request => request.path === "/api/run")).toBe(false);
  const confirmationId = String(proposed.data.confirmation_id);
  gate.grant(confirmationId);
  for await (const _event of adapter.runTurn({ tenant_id: "tenant", session_id: "app-session",
    turn_id: "resume-turn", drawing_id: "rooftop_demo", messages: [],
    confirm: { confirmation_id: confirmationId, approved: true,
      proposal: { tool: "solar-solve-proposal", params, dwg: drawing, capability: "solve" } } })) { /* drain */ }
  expect(requests.filter(request => request.path === "/api/run")).toEqual([{ path: "/api/run", body: {
    tool: "solar-solve-proposal", params, dwg: drawing, solve_context: context,
    catalog_digest: check.args.catalog_digest, catalog_commit: check.args.catalog_commit,
    effective_catalog_digest: check.args.effective_catalog_digest,
    tool_manifest_sha256: check.args.tool_manifest_sha256,
    dwg_version: 3, expected_drawing_head: 3,
  } }]);
  expect(requests.every(request => !request.path.startsWith("/api/drawings/"))).toBe(true);
});

it.each(["revision", "historical", "missing-context"])("refuses canonical solve before approval for %s", async change => {
  const args = { tool: "solar-solve-proposal", params: {},
    ...(change === "missing-context" ? {} : { solve_context: {
      frame_ref: "roof-frame", expected_rev: change === "revision" ? 1 : 17, phase: "initial" } }) };
  const { run, gate, requests } = await setup({ ...state, is_head: change !== "historical" }, 200, args);
  await run("SOLVE").catch(() => []);
  expect(gate.checks.some(call => call.action === "run_solve_tool")).toBe(false);
  expect(requests.some(request => request.path === "/api/run")).toBe(false);
});
it("connects real adapter, loop and HTTP client to canonical summary and Solar write preparation", async () => {
  const { run, requests, gate } = await setup();
  const summary = await run("STATE:summary");
  expect(JSON.stringify(summary)).toContain(version);
  expect(JSON.stringify(summary)).toContain("entity_total");
  await run('RUN:solar-settings PARAMS:{"expected_rev":1,"changes":{"panels_in_sequence":10}}');
  expect(gate.checks.find(c => c.action === "run_write_tool")?.args).toMatchObject({ dwg: drawing,
    drawing_version: 3, expected_drawing_head: 3, input_version_id: version,
    intake_sha256: "a".repeat(64), project_execution_context: state.binding,
    params: { drawing_id: drawing, expected_rev: 17 } });
  expect(requests.every(r => !r.path.startsWith("/api/drawings/"))).toBe(true);
  expect(requests.filter(r => r.path.includes("project-drawing-state")).every(r =>
    JSON.stringify(r.body) === JSON.stringify({ tenant_id: "tenant", session_id: "app-session", turn_id: "app-turn" }))).toBe(true);
});
it.each(["DWG:foreign", 'PARAMS:{"drawing_id":"foreign"}'])("refuses conflicting model drawing before proposal: %s", async conflict => {
  const { run, gate } = await setup();
  await run("RUN:solar-settings " + conflict);
  expect(gate.checks.some(c => c.action === "run_write_tool")).toBe(false);
});
it.each([403, 409, 503])("refuses foreign, revoked or stale authority without legacy fallback: %s", async status => {
  const { run, requests, gate } = await setup({ error: {} }, status);
  await expect(run("RUN:solar-settings")).rejects.toThrow();
  expect(requests).toHaveLength(1);
  expect(gate.checks).toHaveLength(0);
});
it.each([{}, { binding: null, unexpected: true }])("missing or malformed binding is not unbound", async reply => {
  const { run, requests } = await setup(reply);
  await expect(run("STATE:summary")).rejects.toThrow();
  expect(requests).toHaveLength(1);
});
it("explicit authorized unbound response preserves legacy summary", async () => {
  const { run, requests } = await setup({ binding: null });
  expect(JSON.stringify(await run("STATE:summary"))).toContain("rooftop_demo");
  expect(requests.some(r => r.path === "/api/drawings/rooftop_demo/summary")).toBe(true);
});
it("historical canonical selection can read but cannot prepare a current write", async () => {
  const { run, gate } = await setup({ ...state, is_head: false,
    head_version_id: "55555555-5555-4555-8555-555555555555" });
  await run("RUN:solar-settings");
  expect(gate.checks.some(c => c.action === "run_write_tool")).toBe(false);
});

it("confirmation denial remains non-dispatching", async () => {
  const { run, adapter, requests } = await setup();
  const events = await run('RUN:solar-settings PARAMS:{"expected_rev":1,"changes":{"panels_in_sequence":10}}');
  const confirmation = events.find(e => e.type === "proposed_run")!;
  expect(confirmation).toBeDefined();
  for await (const _event of adapter.runTurn({ tenant_id: "tenant", session_id: "app-session",
    turn_id: "app-turn", drawing_id: "rooftop_demo", messages: [],
    confirm: { confirmation_id: String(confirmation.data.confirmation_id), approved: false,
      proposal: { tool: "solar-settings", params: {}, dwg: drawing, capability: "drawing.write" } } })) { /* drain */ }
  expect(requests.every(r => !r.path.includes("jobs") && !r.path.includes("runs"))).toBe(true);
});
it("per-turn authority is copied and cannot drift on the shared client", async () => {
  const { client, requests } = await setup();
  const authority = { tenantId: "tenant", sessionId: "app-session", turnId: "app-turn" };
  const first = client.forTurn(authority);
  authority.turnId = "changed";
  await first.getProjectDrawingState!();
  const second = client.forTurn({ ...authority, sessionId: "other-session" });
  await second.getProjectDrawingState!();
  expect(requests.map(r => r.body)).toEqual([
    { tenant_id: "tenant", session_id: "app-session", turn_id: "app-turn" },
    { tenant_id: "tenant", session_id: "other-session", turn_id: "changed" },
  ]);
});
