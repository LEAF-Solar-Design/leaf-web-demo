import { describe, expect, it } from "vitest";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { SpineConverseRunner, SpineToolResult } from "../src/ports/index.js";

const commit = "a".repeat(40);
const range = { start_line: 501, max_lines: 200, expected_commit: commit };
const source = { contract: "leaf.platform-customize.v1", ref: `refs/leaf/deployed/${commit}`,
  commit, path: "web/src/App shell.jsx", size: 347153, binary: false, content: "<header>\r\n",
  range: { start_line: 501, end_line: 501, next_line: 502, total_lines: 7000, eof: false } };

async function run(args: Record<string, unknown>, gateDenied = false, status = 200, response: unknown = source) {
  const requests: Array<{ url: string; method: string | undefined }> = [];
  const client = new HttpAppRunClient({ baseUrl: "https://app.test", dispatchSecret: "synthetic",
    fetchImpl: async (url, init) => {
      requests.push({ url: String(url), method: init?.method });
      if (new URL(String(url)).pathname !== "/api/platform/source") throw new Error("Unexpected write or unrelated request");
      return Response.json(status === 200 ? response : { error: "source_changed" }, { status });
    } });
  const results: SpineToolResult[] = [];
  const runner: SpineConverseRunner = { async *run(input) {
    results.push(await input.tools.execute("customize_platform", args));
    yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
  } };
  const gate = new FakeGateClient();
  if (gateDenied) gate.deny("read_platform_state", "read denied");
  const store = new FakeSessionStore();
  const loop = new ConverseLoop({ runner, appRun: client, gate, store });
  const session = await loop.createOrGetSession("tenant", "drawing");
  const turn = await loop.handleMessage({ sessionId: session.session_id, tenantId: "tenant",
    text: "Read the current source", contextPacket: {} });
  await turn.done;
  return { requests, results, gate, events: await store.eventsAfter(session.session_id, 0) };
}

describe("commit-bound platform source HTTP transport", () => {
  it("response path carries the exact range through the real loop and HTTP client without a proposal", async () => {
    const { requests, results, gate, events } = await run({ op: "read_source", path: source.path, ...range });
    expect(requests).toEqual([{ method: "GET", url: "https://app.test/api/platform/source?path=web%2Fsrc%2FApp%20shell.jsx" +
      `&start_line=501&max_lines=200&expected_commit=${commit}` }]);
    expect(JSON.parse(results[0]!.content)).toEqual(source);
    expect(results[0]!.isError).toBeUndefined();
    expect(gate.checks).toEqual([expect.objectContaining({ action: "read_platform_state",
      args: { what: "customize_read_source" }, decision: "allow" })]);
    expect(events.some(event => event.type === "confirmation_required" || event.type === "proposed_run")).toBe(false);
  });

  it("response path keeps the legacy query free of range parameters", async () => {
    const { requests, results } = await run({ op: "read_source", path: source.path });
    expect(requests).toEqual([{ method: "GET", url: "https://app.test/api/platform/source?path=web%2Fsrc%2FApp%20shell.jsx" }]);
    expect(JSON.parse(results[0]!.content)).toEqual(source);
  });

  it.each([
    { start_line: 1 },
    { start_line: 1, max_lines: 10 },
    { max_lines: 10, expected_commit: commit },
    { ...range, start_line: 0 },
    { ...range, start_line: 1.5 },
    { ...range, start_line: Number.MAX_SAFE_INTEGER + 1 },
    { ...range, max_lines: 401 },
    { ...range, expected_commit: "A".repeat(40) },
    { ...range, expected_commit: commit + "\n" },
  ])("rejects invalid or partial ranges before gate/HTTP dispatch: %j", async invalid => {
    const { requests, results, gate } = await run({ op: "read_source", path: source.path, ...invalid });
    expect(requests).toEqual([]);
    expect(gate.checks).toEqual([]);
    expect(results[0]!.isError).toBe(true);
  });

  it.each(["list", "list_source", "status", "propose", "land", "unknown"])(
    "does not silently ignore range fields on %s", async op => {
      const { requests, results, gate } = await run({ op, path: source.path, title: "no change", ...range });
      expect(requests).toEqual([]);
      expect(gate.checks).toEqual([]);
      expect(results[0]!.isError).toBe(true);
    });

  it.each([
    { ...source, range: undefined }, // Older app ignored the range query.
    { ...source, commit: "b".repeat(40) },
    { ...source, path: "web/src/other.jsx" },
    { ...source, content: "é".repeat(16385) },
    { ...source, range: { ...source.range, next_line: 999 } },
    { ...source, range: { ...source.range, eof: true } },
  ])("refuses mixed-deployment or invalid range responses: %j", async response => {
    const { requests, results } = await run({ op: "read_source", path: source.path, ...range }, false, 200, response);
    expect(requests).toHaveLength(1);
    expect(results[0]!.isError).toBe(true);
    expect(results[0]!.content).toContain("Invalid platform source range response");
  });

  it("accepts commit-bound empty and metadata-only binary range responses", async () => {
    for (const response of [
      { ...source, size: 0, content: "", range: { start_line: 1, end_line: 0, next_line: null, total_lines: 0, eof: true } },
      { contract: source.contract, ref: source.ref, commit, path: source.path, size: 20, binary: true },
    ]) {
      const { results } = await run({ op: "read_source", path: source.path, ...range, start_line: 1 }, false, 200, response);
      expect(results[0]!.isError).toBeUndefined();
      expect(JSON.parse(results[0]!.content)).toEqual(response);
    }
  });

  it("response path retains read gate denial without dispatching", async () => {
    const { requests, results, gate } = await run({ op: "read_source", path: source.path, ...range }, true);
    expect(requests).toEqual([]);
    expect(gate.checks[0]!.decision).toBe("deny");
    expect(results[0]!.isError).toBe(true);
  });

  it("relays the real server's stale-source refusal instead of accepting content", async () => {
    const { requests, results } = await run({ op: "read_source", path: source.path, ...range }, false, 409);
    expect(requests).toHaveLength(1);
    expect(results[0]!.isError).toBe(true);
    expect(results[0]!.content).toContain("409");
  });
});
