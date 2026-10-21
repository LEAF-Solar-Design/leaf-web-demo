import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpAppRunClient, IntegrationClientError } from "../src/ports/impl/appRunClient.js";
import { IntegrationDatasetsInput, IntegrationQueryInput, IntegrationSaveElementInput } from "../src/ports/index.js";
import { DatasetElementV1, DatasetReadV1, IntegrationActionElementV1 } from "../src/vendor/mushy-author/contracts/integration.js";

const TENANT = "12345678-1234-4234-8234-123456789abc";
const AUTH = { sessionId: "app-session", turnId: "app-turn" };
const SECRET = "synthetic-dispatch-secret+/=";
const CID = "0123456789abcdef";
const REF = { id: "sales", version: 1, digest: "a".repeat(64) };
const FIELD = { id: "amount", label: "Amount", type: { kind: "money", currency: "USD", scale: "minor" }, nullable: true, sensitivity: "internal" };
const QUERY = IntegrationQueryInput.parse({ dataset_ref: REF, query: { fields: ["amount"], limit: 2 } });
const READ = DatasetReadV1.parse({
  schema_version: 1, dataset_ref: REF, fields: [FIELD],
  rows: [{ key: "one", values: { amount: null } }, { key: "two", values: {} }],
  next_cursor: null, status: "ready",
  freshness: { source_updated_at: null, synced_at: "2026-10-20T10:00:00Z", fetched_at: "2026-10-20T10:01:00Z" },
});
const RECIPE = DatasetElementV1.parse({
  schema_version: 1, id: "sales-view", title: "Sales", dataset_ref: REF,
  query: QUERY.query, kind: "dataset.line", props: { x_field: "amount", y_field: "amount" },
});
const SAVE = IntegrationSaveElementInput.parse({ recipe: RECIPE, expectedRevision: 2 });
const ACTION = IntegrationActionElementV1.parse({
  schema_version: 1, id: "sales-action", title: "Create sale", kind: "integration.action",
  connection_id: "sales", connector_ref: REF, operation_ref: { ...REF, id: "create-sale" },
  form: { submit_label: "Create", fields: [{ id: "amount", label: "Amount", kind: "number", required: true, nullable: false, integer: false }] },
});
const ACTION_SAVE = IntegrationSaveElementInput.parse({ recipe: ACTION, expectedRevision: 2 });
const DISCOVERY = { datasets: [{ dataset_ref: REF, title: "Sales", fields: READ.fields, freshness: null, status: "empty" }], next_cursor: null };
const allow = (result: unknown) => ({ gate: { decision: "allow", rung: 0 }, result });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
function setup(body: unknown = allow({ datasets: [], next_cursor: null }), status = 200) {
  const fetchImpl = vi.fn<typeof fetch>(async () => response(body, status));
  const client = new HttpAppRunClient({ baseUrl: "https://app.invalid/", dispatchSecret: SECRET, fetchImpl });
  return { client, fetchImpl };
}

afterEach(() => vi.useRealTimers());

describe("bounded integration HTTP transport", () => {
  it.each([undefined, 2])("saves an inert action through the fixed host endpoint (revision %s)", async (expectedRevision) => {
    const input = IntegrationSaveElementInput.parse({ recipe: ACTION, ...(expectedRevision === undefined ? {} : { expectedRevision }) });
    const { client, fetchImpl } = setup(allow({ element: ACTION, revision: 3 }));
    expect((await client.integrationSaveElement(TENANT, input, AUTH)).result).toEqual({ element: ACTION, revision: 3 });
    expect(fetchImpl).toHaveBeenCalledExactlyOnceWith("https://app.invalid/internal/integrations/elements", expect.objectContaining({
      method: "POST", redirect: "error", body: JSON.stringify(input),
      headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-dispatch-secret": SECRET,
        "x-authority-session-id": AUTH.sessionId, "x-authority-turn-id": AUTH.turnId },
    }));
  });

  it.each([
    { ...ACTION, kind: "integration.run" }, { ...ACTION, grant: "model-grant" },
    { ...ACTION, operation_ref: { ...REF, version: 0 } },
    { ...ACTION, connector_ref: { ...REF, digest: "bad" } },
    { ...ACTION, form: { ...ACTION.form, arguments: { amount: 1 } } },
  ])("rejects invalid action recipes before fetch: %j", async (recipe) => {
    const { client, fetchImpl } = setup();
    await expect(client.integrationSaveElement(TENANT, { recipe } as never, AUTH)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each([
    { element: { ...ACTION, operation_ref: { ...REF, version: 2 } }, revision: 3 },
    { element: { ...ACTION, connector_ref: { ...REF, digest: "b".repeat(64) } }, revision: 3 },
    { element: { ...ACTION, form: { ...ACTION.form, submit_label: "Changed" } }, revision: 3 },
    { element: ACTION, revision: 0 }, { element: ACTION, revision: Number.MAX_SAFE_INTEGER + 1 },
    { element: ACTION, revision: 3, secret: SECRET },
  ])("rejects action response drift or reflection without retrying: %j", async (saved) => {
    const { client, fetchImpl } = setup(allow(saved));
    await expect(client.integrationSaveElement(TENANT, ACTION_SAVE, AUTH)).rejects.toMatchObject({ code: "outcome_unknown" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("uses fixed paths, POST, headers, normalized JSON, and one call per operation", async () => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(response(allow(DISCOVERY)));
    fetchImpl.mockResolvedValueOnce(response(allow(READ)));
    fetchImpl.mockResolvedValueOnce(response(allow({ element: RECIPE, revision: 3 })));
    const discovery = await client.integrationDatasets(TENANT, IntegrationDatasetsInput.parse({}), AUTH);
    const read = await client.integrationQuery(TENANT, QUERY, AUTH);
    const saved = await client.integrationSaveElement(TENANT, SAVE, AUTH);
    expect(discovery).toEqual({ gate: { decision: "allow", rung: "R0" }, result: DISCOVERY });
    expect(read.result).toEqual(READ);
    expect(saved.result).toEqual({ element: RECIPE, revision: 3 });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
    const paths = ["datasets", "query", "elements"];
    const bodies = [{ limit: 50 }, QUERY, SAVE];
    fetchImpl.mock.calls.forEach(([url, init], index) => {
      expect(url).toBe(`https://app.invalid/internal/integrations/${paths[index]}`);
      expect(init?.method).toBe("POST");
      expect(init?.redirect).toBe("error");
      expect(init?.signal).toBeInstanceOf(AbortSignal);
      expect(JSON.parse(String(init?.body))).toEqual(bodies[index]);
      expect(init?.headers).toEqual({
        "content-type": "application/json", "x-tenant-id": TENANT,
        "x-dispatch-secret": SECRET, "x-authority-session-id": AUTH.sessionId,
        "x-authority-turn-id": AUTH.turnId,
      });
    });
  });

  it.each(["deny", "awaiting_approval"] as const)("projects %s with no result", async (decision) => {
    const gate = { decision, rung: 7, policy: "always_confirm", reason: "policy", ...(decision === "awaiting_approval" ? { confirmation_id: CID } : {}) };
    const { client, fetchImpl } = setup({ gate });
    expect(await client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).toEqual({ gate: { ...gate, rung: "R7" } });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["not-a-uuid", AUTH], [TENANT.toUpperCase(), AUTH], ["00000000-0000-0000-0000-000000000000", AUTH],
    [TENANT, { sessionId: "", turnId: "turn" }], [TENANT, { sessionId: "session\r\ninjected", turnId: "turn" }],
    [TENANT, { sessionId: "session", turnId: "x".repeat(201) }], [TENANT, { sessionId: " session", turnId: "turn" }],
    [TENANT, { sessionId: "session", turnId: "turn\u0085id" }],
  ] as const)("rejects invalid tenant or authority before fetch: %s", async (tenant, authority) => {
    const { client, fetchImpl } = setup();
    await expect(client.integrationDatasets(tenant, { limit: 50 }, authority)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each([{ limit: 51 }, { limit: 50, path: "https://attacker.invalid" }, { limit: 50, headers: { authorization: "private" } }, { limit: 50, operation: "datasets" }])("rejects extra transport fields and invalid inputs: %j", async (input) => {
    const { client, fetchImpl } = setup();
    await expect(client.integrationDatasets(TENANT, input as never, AUTH)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("rejects a request over 128 KiB without sending it", async () => {
    const { client, fetchImpl } = setup();
    const input = IntegrationQueryInput.parse({ ...QUERY, query: { ...QUERY.query, filters: [{ field: "amount", op: "eq", value: "é".repeat(70 * 1024) }] } });
    await expect(client.integrationQuery(TENANT, input, AUTH)).rejects.toMatchObject({ code: "invalid_input" });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("accepts a request at the 128 KiB byte boundary", async () => {
    const input = IntegrationQueryInput.parse({ ...QUERY, query: { ...QUERY.query, filters: [{ field: "amount", op: "eq", value: "" }] } });
    const padding = 128 * 1024 - Buffer.byteLength(JSON.stringify(input));
    input.query.filters![0] = { field: "amount", op: "eq", value: "x".repeat(padding) };
    expect(Buffer.byteLength(JSON.stringify(input))).toBe(128 * 1024);
    const { client, fetchImpl } = setup(allow(READ));
    expect((await client.integrationQuery(TENANT, input, AUTH)).result).toEqual(READ);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("refuses redirect/network failures safely without retrying", async () => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockRejectedValueOnce(new TypeError(`redirect https://private.invalid ${SECRET}`));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ message: "Integration request failed: unavailable.", code: "unavailable" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][1]?.redirect).toBe("error");
  });

  it("caps streamed response bytes, aborts, cancels the reader, and clears its timer", async () => {
    vi.useFakeTimers();
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array(256 * 1024 + 1)); }, cancel,
    });
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(stream));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "response_too_large" });
    expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("accepts exactly 256 KiB of streamed UTF-8 JSON without losing split characters", async () => {
    const read = {
      ...READ, fields: [{ ...READ.fields[0], type: { kind: "text" as const } }],
      rows: [{ key: "one", values: { amount: "é" } }],
    };
    const padding = 256 * 1024 - Buffer.byteLength(JSON.stringify(allow(read)));
    read.rows[0].values.amount += "x".repeat(padding);
    const bytes = new TextEncoder().encode(JSON.stringify(allow(read)));
    expect(bytes.byteLength).toBe(256 * 1024);
    const split = bytes.indexOf(0xc3) + 1;
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(bytes.slice(0, split));
      controller.enqueue(bytes.slice(split));
      controller.close();
    } });
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(stream));
    expect((await client.integrationQuery(TENANT, QUERY, AUTH)).result).toEqual(read);
  });

  it("counts all chunks even when the declared content length is small", async () => {
    const cancel = vi.fn();
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array(256 * 1024));
      controller.enqueue(new Uint8Array(1));
    }, cancel });
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(stream, { headers: { "content-length": "1" } }));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "response_too_large" });
    expect(cancel).toHaveBeenCalledTimes(1);
  });

  it("uses a single 15-second deadline through slow body consumption", async () => {
    vi.useFakeTimers();
    const cancel = vi.fn();
    const { client, fetchImpl } = setup();
    fetchImpl.mockImplementationOnce(async () => {
      await new Promise((resolve) => setTimeout(resolve, 5_000));
      return new Response(new ReadableStream<Uint8Array>({ cancel }));
    });
    const pending = client.integrationDatasets(TENANT, { limit: 50 }, AUTH);
    const assertion = expect(pending).rejects.toMatchObject({ code: "timeout", message: "Integration request failed: timeout." });
    await vi.advanceTimersByTimeAsync(14_999);
    expect(cancel).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1);
    await assertion;
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out even if an injected fetch ignores its AbortSignal", async () => {
    vi.useFakeTimers();
    const { client, fetchImpl } = setup();
    fetchImpl.mockImplementationOnce(() => new Promise(() => {}));
    const assertion = expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "timeout" });
    await vi.advanceTimersByTimeAsync(15_000);
    await assertion;
    expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("clears the deadline after successful body consumption", async () => {
    vi.useFakeTimers();
    const { client } = setup();
    await client.integrationDatasets(TENANT, { limit: 50 }, AUTH);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["not-json", "", "{\"gate\":", "null"])("rejects malformed response text safely: %s", async (text) => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(text));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
    expect(fetchImpl.mock.calls[0][1]?.signal?.aborted).toBe(true);
  });

  it("rejects malformed UTF-8 rather than replacing bytes in typed data", async () => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(new Uint8Array([0xff])));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    { gate: { decision: "other" } }, { gate: { decision: "allow" } },
    { gate: { decision: "awaiting_approval" } }, { gate: { decision: "deny" }, result: null },
    { gate: { decision: "allow", rung: "R0" }, result: { datasets: [], next_cursor: null } },
    { gate: { decision: "allow", confirmation_id: CID }, result: { datasets: [], next_cursor: null } },
    { gate: { decision: "allow", rung: 8 }, result: { datasets: [], next_cursor: null } },
    { gate: { decision: "deny", receipt: "private" } },
    { ...allow({ datasets: [], next_cursor: null }), receipt: "private" },
  ])("strictly rejects malformed wire envelopes: %j", async (body) => {
    const { client } = setup(body);
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    { ...READ, dataset_ref: { ...REF, digest: "b".repeat(64) } },
    { ...READ, rows: [{ key: "one", values: { amount: 1.5 } }] },
    { ...READ, fields: [{ ...READ.fields[0], nullable: false }] },
    { ...READ, freshness: { ...READ.freshness, fetched_at: "2026-02-30T00:00:00Z" } },
    { ...READ, status: "empty" },
    { ...READ, connection: { secret: "private" } },
  ])("preserves canonical query refinements and reference identity: %j", async (read) => {
    const { client } = setup(allow(read));
    await expect(client.integrationQuery(TENANT, QUERY, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    { datasets: [DISCOVERY.datasets[0], DISCOVERY.datasets[0]], next_cursor: null },
    { datasets: [{ ...DISCOVERY.datasets[0], fields: [{ ...FIELD, type: { kind: "bogus" } }] }], next_cursor: null },
    { datasets: [{ ...DISCOVERY.datasets[0], freshness: { ...READ.freshness, synced_at: "yesterday" } }], next_cursor: null },
    { datasets: [], next_cursor: "x".repeat(1025) },
    { datasets: [{ ...DISCOVERY.datasets[0], connection_id: "private" }], next_cursor: null },
  ])("bounds and validates discovery pages: %j", async (page) => {
    const { client } = setup(allow(page));
    await expect(client.integrationDatasets(TENANT, { limit: 1 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each([
    { element: RECIPE, revision: 0 }, { element: RECIPE, revision: 1.5 },
    { element: RECIPE, revision: Number.MAX_SAFE_INTEGER + 1 },
    { element: { ...RECIPE, title: "different recipe" }, revision: 3 },
  ])("requires an exact saved recipe and positive safe revision: %j", async (saved) => {
    const { client } = setup(allow(saved));
    await expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({ code: "outcome_unknown" });
  });

  it.each([
    SECRET, encodeURIComponent(SECRET), encodeURIComponent(encodeURIComponent(SECRET)),
    Buffer.from(SECRET).toString("base64"), Buffer.from(SECRET).toString("base64url"),
    Buffer.from(SECRET).toString("hex"), [...SECRET].map((char) => `&#${char.charCodeAt(0)};`).join(""),
  ])("rejects secret reflection before exposing gate metadata: %s", async (reflected) => {
    const { client } = setup({ gate: { decision: "deny", reason: reflected } });
    let caught: unknown;
    try { await client.integrationDatasets(TENANT, { limit: 50 }, AUTH); } catch (error) { caught = error; }
    expect(caught).toBeInstanceOf(IntegrationClientError);
    expect(caught).toMatchObject({ code: "invalid_response", message: "Integration request failed: invalid_response." });
    expect(JSON.stringify(caught)).not.toContain(SECRET);
    expect(String(caught)).not.toContain(reflected);
  });

  it("rejects JSON unicode escape reflection and reflection in discarded extras", async () => {
    const { client, fetchImpl } = setup();
    const escaped = [...SECRET].map((char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`).join("");
    fetchImpl.mockResolvedValueOnce(new Response(`{"gate":{"decision":"deny","reason":"${escaped}"}}`));
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
    fetchImpl.mockResolvedValueOnce(response({ ...allow(READ), receipt: SECRET }));
    await expect(client.integrationQuery(TENANT, QUERY, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it.each(["invalid_input", "unauthenticated", "denied", "unavailable", "conflict", "invalid_response", "timeout", "response_too_large", "outcome_unknown"])("maps host error %s to a fixed safe category", async (code) => {
    const { client, fetchImpl } = setup({ error: { code, correlationId: TENANT } }, 503);
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code,
      message: code === "outcome_unknown" ? "The save outcome is unknown. Inspect saved views before retrying." : `Integration request failed: ${code}.`,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it.each([{ error: { code: "raw-private-text", correlationId: TENANT } }, { error: { code: "unavailable", correlationId: "bad" } }, { error: { code: "unavailable", correlationId: TENANT, message: SECRET } }])("does not relay malformed failure envelopes", async (body) => {
    const { client } = setup(body, 500);
    await expect(client.integrationDatasets(TENANT, { limit: 50 }, AUTH)).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("preserves definite save conflict and never retries", async () => {
    const { client, fetchImpl } = setup({ error: { code: "conflict", correlationId: TENANT } }, 409);
    await expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({ code: "conflict" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("preserves a definite missing-resource rejection without retrying", async () => {
    const { client, fetchImpl } = setup({ error: { code: "not_found", correlationId: TENANT } }, 404);
    await expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({ code: "not_found" });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not interpret a malformed save response as a safe retry", async () => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response("truncated", { status: 200 }));
    await expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({
      code: "outcome_unknown", message: "The save outcome is unknown. Inspect saved views before retrying.",
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("treats ambiguous save network failure as outcome unknown", async () => {
    const { client, fetchImpl } = setup();
    fetchImpl.mockRejectedValueOnce(new Error(`lost response ${SECRET}`));
    await expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({ code: "outcome_unknown", message: "The save outcome is unknown. Inspect saved views before retrying." });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("cancels a stalled save body with inspect-before-retry guidance", async () => {
    vi.useFakeTimers();
    const cancel = vi.fn();
    const { client, fetchImpl } = setup();
    fetchImpl.mockResolvedValueOnce(new Response(new ReadableStream<Uint8Array>({ cancel })));
    const assertion = expect(client.integrationSaveElement(TENANT, SAVE, AUTH)).rejects.toMatchObject({ code: "outcome_unknown", message: "The save outcome is unknown. Inspect saved views before retrying." });
    await vi.advanceTimersByTimeAsync(15_000);
    await assertion;
    expect(cancel).toHaveBeenCalledTimes(1);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
