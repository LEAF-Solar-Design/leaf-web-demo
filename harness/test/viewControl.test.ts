import { describe, expect, it } from "vitest";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import { SPINE_TOOL_NAMES, ViewControlInput } from "../src/ports/index.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";

describe("view_control", () => {
  it("exposes four operations and requires a layer only for zoom_to_layer", () => {
    expect(SPINE_TOOL_NAMES).toContain("view_control");
    for (const op of ["fit", "zoom_in", "zoom_out"]) {
      expect(ViewControlInput.safeParse({ op }).success).toBe(true);
      expect(ViewControlInput.safeParse({ op, layer: "ROOF" }).success).toBe(false);
    }
    expect(ViewControlInput.safeParse({ op: "zoom_to_layer", layer: "R".repeat(255) }).success).toBe(true);
    for (const layer of [undefined, "", "R".repeat(256), "R\n", "R\u007f", 42]) {
      expect(ViewControlInput.safeParse({ op: "zoom_to_layer", layer }).success).toBe(false);
    }
    expect(ViewControlInput.safeParse({ op: "pan" }).success).toBe(false);
    expect(ViewControlInput.safeParse({ op: "fit", unknown: true }).success).toBe(false);
  });

  it("consults the read gate and emits the layer request for the client", async () => {
    const appRun = new FakeAppRunClient();
    const gate = new FakeGateClient();
    const store = new FakeSessionStore();
    const tenantId = "11111111-1111-4111-8111-111111111111";
    const args = { op: "zoom_to_layer", layer: "ROOF" };
    const request = { contract: "leaf.view-request.v1", ...args };
    const replies: string[] = [];
    const loop = new ConverseLoop({
      appRun, gate, store,
      runner: { async *run(input) {
        replies.push((await input.tools.execute("view_control", args)).content);
        yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
      } },
    });
    const session = await loop.createOrGetSession(tenantId, "drawing-a");
    const turn = await loop.handleMessage({
      sessionId: session.session_id, tenantId, text: "Show the roof",
      contextPacket: { catalog: [], drawing: { id: "drawing-a", head_version: 1 }, grant: { kind: "oauth", degraded: false } },
    });
    await turn.done;
    expect(gate.checks).toEqual([expect.objectContaining({ action: "read_platform_state", args: { what: "capabilities" } })]);
    expect(appRun.methodLog).toEqual([]);
    expect(replies.map(reply => JSON.parse(reply))).toEqual([{ ...request, applied_by: "client" }]);
    const events = await store.eventsAfter(session.session_id, 0);
    const event = events.find(event => event.type === "tool_result");
    expect(event?.data).toMatchObject({ tool: "view_control", ok: true });
    expect(event?.data.result).toEqual(request);
  });
});
