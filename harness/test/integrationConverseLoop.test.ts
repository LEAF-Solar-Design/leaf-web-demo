import { describe, expect, it, vi } from "vitest";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import { IntegrationClientError } from "../src/ports/impl/appRunClient.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { INTEGRATION_TOOL_INPUTS } from "../src/ports/index.js";
import type { AppRunClient, ConverseRunInput, SpineConverseRunner, SpineToolResult } from "../src/ports/index.js";
import { DatasetReadV1, DatasetElementV1, IntegrationActionElementV1 } from "../src/vendor/mushy-author/contracts/integration.js";

const TENANT = "12345678-1234-4234-8234-123456789abc";
const CID = "0123456789abcdef";
const REF = { id: "sales", version: 1, digest: "a".repeat(64) };
const QUERY = { fields: ["amount"], limit: 2, filters: [{ field: "amount", op: "is_null" }], sort: [{ field: "amount", direction: "desc" }] };
const READ = DatasetReadV1.parse({
  schema_version: 1, dataset_ref: REF,
  fields: [{ id: "amount", label: "Amount", type: { kind: "number", unit: "kWh", precision: 2 }, nullable: true, sensitivity: "internal" }],
  rows: [{ key: "one", values: { amount: null } }, { key: "two", values: {} }],
  next_cursor: null, status: "ready",
  freshness: { source_updated_at: null, synced_at: "2026-10-20T10:00:00Z", fetched_at: "2026-10-20T10:01:00Z" },
});
const RECIPE = DatasetElementV1.parse({
  schema_version: 1, id: "sales-view", title: "Sales", dataset_ref: REF,
  query: QUERY, kind: "dataset.table", props: { columns: ["amount"], row_limit: 2 },
});
const ACTION = IntegrationActionElementV1.parse({
  schema_version: 1, id: "sales-view", title: "Create sale", kind: "integration.action",
  connection_id: "sales", connector_ref: REF, operation_ref: { ...REF, id: "create-sale" },
  form: { submit_label: "Create", fields: [{ id: "amount", label: "Amount", kind: "number", required: true, nullable: false, integer: false }] },
});
const CATALOG = {
  actions: [{ connection_id: ACTION.connection_id, connection_title: "Sales", connector_ref: ACTION.connector_ref,
    operation_ref: ACTION.operation_ref, title: ACTION.title, form: ACTION.form }],
  next_cursor: "opaque_1",
};
const CASES = [
  { tool: "integration_datasets", args: {}, normalized: { limit: 50 }, result: { datasets: [], next_cursor: null } },
  { tool: "integration_query", args: { dataset_ref: REF, query: QUERY }, normalized: { dataset_ref: REF, query: QUERY }, result: READ },
  { tool: "integration_save_element", args: { recipe: RECIPE, expectedRevision: 3 }, normalized: { recipe: RECIPE, expectedRevision: 3 }, result: { element: RECIPE, revision: 4 } },
  { tool: "integration_save_element", args: { recipe: ACTION, expectedRevision: 3 }, normalized: { recipe: ACTION, expectedRevision: 3 }, result: { element: ACTION, revision: 4 } },
  { tool: "integration_action_catalog", args: {}, normalized: { limit: 50 }, result: CATALOG },
  { tool: "integration_action_catalog", args: { limit: 1, after: "opaque_0" }, normalized: { limit: 1, after: "opaque_0" }, result: { actions: [], next_cursor: "opaque_1" } },
] as const;

function setup(tool: string, args: Record<string, unknown>, response: unknown, connected = true) {
  const store = new FakeSessionStore();
  const gate = new FakeGateClient();
  const appRun: AppRunClient = new FakeAppRunClient();
  const host = vi.fn(async () => response as never);
  const wrongMethod = vi.fn(async () => { throw new Error("wrong integration method"); });
  if (connected) {
    appRun.integrationDatasets = tool === "integration_datasets" ? host : wrongMethod;
    appRun.integrationActionCatalog = tool === "integration_action_catalog" ? host : wrongMethod;
    appRun.integrationQuery = tool === "integration_query" ? host : wrongMethod;
    appRun.integrationSaveElement = tool === "integration_save_element" ? host : wrongMethod;
  }
  const results: SpineToolResult[] = [];
  const runs: ConverseRunInput[] = [];
  const runner: SpineConverseRunner = {
    async *run(input) {
      runs.push(input);
      let actualTool = tool, actualArgs = args;
      if (input.userMessage.includes("=== CONFIRMATION ===")) {
        actualTool = /action: (\S+)/.exec(input.userMessage)![1];
        actualArgs = { ...JSON.parse(/args: (\{.*\})/.exec(input.userMessage)![1]), confirmation_id: CID };
      }
      const permission = await input.canUseTool(`mcp__spine__${actualTool}`, actualArgs);
      expect(permission.behavior).toBe("allow");
      results.push(await input.tools.execute(actualTool, actualArgs));
      yield { type: "done", stopReason: "end_turn", sdkSessionId: "script-session" };
    },
  };
  const loop = new ConverseLoop({ appRun, gate, store, runner });
  const send = async (authority: { authoritySessionId?: string; authorityTurnId?: string } = { authoritySessionId: "app-session", authorityTurnId: "app-turn-1" }, confirm = false) => {
    const session = await loop.createOrGetSession(TENANT, "drawing");
    const turn = await loop.handleMessage({
      sessionId: session.session_id, tenantId: TENANT, contextPacket: {}, ...authority,
      ...(confirm ? { confirm: { confirmationId: CID, approved: true } } : { text: "perform action" }),
    });
    await turn.done;
    return session;
  };
  return { store, gate, host, results, runs, send };
}

describe("integration tools use the host's single permission gate", () => {
  it.each(CASES)("$tool returns the host result with exactly one call", async (entry) => {
    expect(INTEGRATION_TOOL_INPUTS[entry.tool].parse(entry.args)).toEqual(entry.normalized);
    const state = setup(entry.tool, entry.args, { gate: { decision: "allow", rung: "R0" }, result: entry.result });
    await state.send();
    expect(state.host).toHaveBeenCalledExactlyOnceWith(TENANT, entry.normalized, { sessionId: "app-session", turnId: "app-turn-1" });
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results).toEqual([{ content: JSON.stringify(entry.result) }]);
    const events = [...state.store.events.values()].flat();
    const summaries = events.filter((event) => event.type === "tool_result").map((event) => event.data.summary);
    expect(summaries).toEqual([entry.tool === "integration_action_catalog" ? `${entry.result.actions.length} action forms` : entry.tool === "integration_datasets" ? "0 datasets" : entry.tool === "integration_query" ? "2 rows (ready)" : "element sales-view revision 4"]);
  });

  it.each(CASES)("$tool mirrors exact normalized arguments and replays with fresh authority", async (entry) => {
    const state = setup(entry.tool, entry.args, { gate: { decision: "awaiting_approval", confirmation_id: CID, policy: "always_confirm", rung: "R2" } });
    const session = await state.send();
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
    const mirror = state.store.confirmations.get(CID)!;
    expect(JSON.parse(mirror.args_json)).toEqual(entry.normalized);
    expect(Object.keys(JSON.parse(mirror.args_json)).filter((key) => ["operation", "authority", "grant", "token", "confirmation_id"].includes(key))).toEqual([]);
    const events = state.store.events.get(session.session_id)!;
    expect(events.find((event) => event.type === "confirmation_required")?.data.payload).toEqual(entry.normalized);
    expect(events.find((event) => event.type === "turn_complete")?.data.stop_reason).toBe("awaiting_approval");
    expect(JSON.parse(state.results[0].content)).toEqual({ pending: true, confirmation_id: CID });
    state.host.mockResolvedValueOnce({ gate: { decision: "allow" }, result: entry.result } as never);
    await state.send({ authoritySessionId: "app-session", authorityTurnId: "app-turn-2" }, true);
    expect(state.host).toHaveBeenCalledTimes(2);
    expect(state.host).toHaveBeenLastCalledWith(TENANT, { ...entry.normalized, confirmation_id: CID }, { sessionId: "app-session", turnId: "app-turn-2" });
    expect(state.runs[1].userMessage).toContain(mirror.args_json);
    expect(state.runs[1].userMessage).not.toContain("app-turn-1");
    expect(JSON.parse(state.results[1].content)).toEqual(entry.result);
    expect(state.gate.checks).toHaveLength(0);
  });

  it.each(CASES)("$tool relays deny without an additional request", async (entry) => {
    const state = setup(entry.tool, entry.args, { gate: { decision: "deny", reason: "policy", policy: "deny", rung: "R3" } });
    await state.send();
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0].isError).toBe(true);
    expect(JSON.parse(state.results[0].content)).toEqual({ denied: true, reason: "policy", policy: "deny", rung: "R3" });
    expect(state.store.confirmations.size).toBe(0);
  });

  it("ordinary tools still consult their gate once", async () => {
    const state = setup("drawing_state", {}, undefined);
    await state.send();
    expect(state.gate.checks).toHaveLength(1);
    expect(state.gate.checks[0].action).toBe("read_platform_state");
    expect(state.host).not.toHaveBeenCalled();
    expect(state.results[0].isError).not.toBe(true);
  });

  it("catalog pending mirrors discard a previous confirmation id", async () => {
    const state = setup("integration_action_catalog", { after: "opaque_0", confirmation_id: CID },
      { gate: { decision: "awaiting_approval", confirmation_id: CID } });
    await state.send();
    expect(JSON.parse(state.store.confirmations.get(CID)!.args_json)).toEqual({ limit: 50, after: "opaque_0" });
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
  });

  it.each([{}, { authoritySessionId: "app-session" }, { authoritySessionId: "app-session", authorityTurnId: "bad\n" }])("catalog requires trusted authority: %j", async (authority) => {
    const state = setup("integration_action_catalog", {}, undefined);
    await state.send(authority);
    expect(state.host).not.toHaveBeenCalled();
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0].content).toBe("Integration request failed: unavailable.");
  });

  it.each([
    { limit: 0 }, { limit: 51 }, { limit: 1.5 }, { after: "" }, { after: "bad\n" },
    { after: "a".repeat(1025) }, { authorityTurnId: "forged" }, { operation: "action_catalog" },
    { url: "https://foreign.invalid" }, { confirmation_id: "invalid" },
  ])("rejects catalog argument injection: %j", async (args) => {
    const state = setup("integration_action_catalog", args, undefined);
    await state.send();
    expect(state.host).not.toHaveBeenCalled();
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0].content).toBe("Integration request failed: invalid_input.");
  });

  it.each([
    { datasets: [], next_cursor: null }, { element: ACTION, revision: 1 },
    { ...CATALOG, actions: [CATALOG.actions[0], CATALOG.actions[0]] },
    { ...CATALOG, next_cursor: "bad cursor" },
    { ...CATALOG, actions: [{ ...CATALOG.actions[0], authority: "forged" }] },
    { ...CATALOG, actions: [{ ...CATALOG.actions[0], form: { ...ACTION.form, arguments: {} } }] },
  ])("rejects malformed catalog results without a second host call: %j", async (result) => {
    const state = setup("integration_action_catalog", { limit: 1 }, { gate: { decision: "allow" }, result });
    await state.send();
    expect(state.results[0].content).toBe("Integration request failed: invalid_response.");
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
  });

  it("catalog transport failure is safe and never automatically retried", async () => {
    const state = setup("integration_action_catalog", {}, undefined);
    state.host.mockRejectedValueOnce(new Error("synthetic-private-token"));
    await state.send();
    expect(state.results[0].content).toBe("Integration request failed: unavailable.");
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
    expect(JSON.stringify([...state.store.events.values()])).not.toContain("synthetic-private-token");
  });

  it.each([{}, { authoritySessionId: "app-session" }, { authorityTurnId: "app-turn" }, { authoritySessionId: "bad\n", authorityTurnId: "turn" }])("requires trusted authority: %j", async (authority) => {
    const state = setup("integration_datasets", {}, undefined);
    await state.send(authority);
    expect(state.host).not.toHaveBeenCalled();
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: unavailable." });
  });

  it.each(CASES)("$tool fails unavailable when its client method is absent", async (entry) => {
    const state = setup(entry.tool, entry.args, undefined, false);
    await state.send();
    expect(state.results[0].content).toBe("Integration request failed: unavailable.");
    expect(state.gate.checks).toHaveLength(0);
  });

  it.each([
    { ...ACTION, kind: "integration.run" },
    { ...ACTION, authority: "model-authority" },
    { ...ACTION, operation_ref: { ...REF, id: "../run" } },
    { ...ACTION, form: { ...ACTION.form, arguments: { amount: 1 } } },
  ])("rejects malformed action recipes before the host: %j", async (recipe) => {
    const state = setup("integration_save_element", { recipe }, undefined);
    await state.send();
    expect(state.host).not.toHaveBeenCalled();
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: invalid_input." });
  });

  it.each([
    { operation: "datasets" }, { authoritySessionId: "model-authority" }, { limit: 51 },
    { confirmation_id: "not-a-gate-id" }, { after: "bad cursor" },
  ])("rejects malformed tool arguments before the host: %j", async (args) => {
    const state = setup("integration_datasets", args, undefined);
    await state.send();
    expect(state.host).not.toHaveBeenCalled();
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: invalid_input." });
  });

  it.each([
    null, {}, { gate: { decision: "maybe" } }, { gate: { decision: "allow" } },
    { gate: { decision: "allow" }, result: null },
    { gate: { decision: "allow", confirmation_id: CID }, result: { datasets: [], next_cursor: null } },
    { gate: { decision: "deny" }, result: {} },
    { gate: { decision: "awaiting_approval" } },
    { gate: { decision: "awaiting_approval", confirmation_id: CID }, result: {} },
    { gate: { decision: "awaiting_approval", confirmation_id: "ABCDEF0123456789" } },
    { gate: { decision: "allow", rung: "R8" }, result: { datasets: [], next_cursor: null } },
    { gate: { decision: "deny", grant: "private" } },
  ])("fails closed on malformed host verdicts: %j", async (response) => {
    const state = setup("integration_datasets", {}, response);
    await state.send();
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: invalid_response." });
    expect(state.store.confirmations.size).toBe(0);
  });

  it.each([
    { tool: "integration_query", args: CASES[1].args, result: { ...READ, dataset_ref: { ...REF, version: 2 } } },
    { tool: "integration_query", args: CASES[1].args, result: { ...READ, rows: [{ key: "x", values: { amount: "bad" } }] } },
    { tool: "integration_save_element", args: CASES[2].args, result: { element: { ...RECIPE, title: "different" }, revision: 4 } },
    { tool: "integration_save_element", args: CASES[2].args, result: { element: RECIPE, revision: 0 } },
    { tool: "integration_save_element", args: CASES[3].args, result: { element: { ...ACTION, operation_ref: { ...REF, version: 2 } }, revision: 4 } },
    { tool: "integration_save_element", args: CASES[3].args, result: { element: { ...ACTION, form: { ...ACTION.form, submit_label: "Changed" } }, revision: 4 } },
  ])("validates typed results for $tool", async ({ tool, args, result }) => {
    const state = setup(tool, args, { gate: { decision: "allow" }, result });
    await state.send();
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: invalid_response." });
    expect(state.host).toHaveBeenCalledTimes(1);
  });

  it("does not expose arbitrary client exceptions in results or events", async () => {
    const secret = "synthetic-dispatch-secret";
    const state = setup("integration_datasets", {}, undefined);
    state.host.mockRejectedValueOnce(new Error(`https://private.invalid ${secret}`));
    await state.send();
    expect(state.results[0]).toMatchObject({ isError: true, content: "Integration request failed: unavailable." });
    expect(JSON.stringify([...state.store.events.values()])).not.toContain(secret);
  });

  it("relays explicit unknown-save guidance without retrying", async () => {
    const state = setup("integration_save_element", CASES[2].args, undefined);
    state.host.mockRejectedValueOnce(new IntegrationClientError("outcome_unknown"));
    await state.send();
    expect(state.results[0]).toMatchObject({ isError: true, content: "The save outcome is unknown. Inspect saved views before retrying." });
    expect(state.host).toHaveBeenCalledTimes(1);
    expect(state.gate.checks).toHaveLength(0);
  });
});
