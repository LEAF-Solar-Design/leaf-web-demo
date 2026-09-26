import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { ConverseRunner, ConverseTurnInput, HarnessTurnEvent } from "../src/ports/index.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const FIXTURE = join(dirname(fileURLToPath(import.meta.url)), "fixtures", "tenant-repo");
const ERROR = "context_packet must be an object of at most 16384 serialized chars";
let server: Server | undefined;

afterEach(() => {
  server?.closeAllConnections();
  server?.close();
  server = undefined;
});

function recordingHarness() {
  const inputs: ConverseTurnInput[] = [];
  const converseRunner: ConverseRunner = {
    async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
      inputs.push(input);
      yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
    },
  };
  server = createHarness({
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo: new FakeTenantRepoProvider(FIXTURE),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
    converseRunner,
  }).listen(0);
  const { port } = server.address() as AddressInfo;
  const post = (extra: Record<string, unknown> = {}) => fetch(`http://127.0.0.1:${port}/turn`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      tenant_id: "acme", session_id: "session-1", turn_id: "turn-1",
      drawing_id: "drawing-1", messages: [], text: "hello", ...extra,
    }),
  });
  return { inputs, post };
}

describe("ContextPacket on the live turn wire", () => {
  it("POST /turn hands a context_packet object to the runner unchanged", async () => {
    const { inputs, post } = recordingHarness();
    const context_packet = {
      catalog: [{ name: "panel-count", capabilities: ["drawing.read"] }],
      drawing: { id: "drawing-1", layers: [], entity_total: 0 },
      entitlements: { converse: true },
      classifier_hint: { lane: "run" },
      grant: { kind: "missing", degraded: true },
    };
    const response = await post({ context_packet });
    expect(response.status).toBe(200);
    await response.text();
    expect(inputs).toHaveLength(1);
    expect(inputs[0].context_packet).toEqual(context_packet);

    // The inclusive boundary is measured after JSON serialization.
    const boundary = { text: "x".repeat(16_384 - JSON.stringify({ text: "" }).length) };
    const edge = await post({ context_packet: boundary });
    expect(edge.status).toBe(200);
    await edge.text();
    expect(inputs[1].context_packet).toEqual(boundary);
  });

  it("POST /turn without context_packet leaves the field absent", async () => {
    const { inputs, post } = recordingHarness();
    const response = await post();
    expect(response.status).toBe(200);
    await response.text();
    expect(inputs).toHaveLength(1);
    expect(Object.prototype.hasOwnProperty.call(inputs[0], "context_packet")).toBe(false);
  });

  it("POST /turn rejects a non-object context_packet with 400", async () => {
    const { inputs, post } = recordingHarness();
    for (const context_packet of [null, [], "packet", 42, true]) {
      const response = await post({ context_packet });
      expect(response.status).toBe(400);
      expect(await response.text()).toContain(ERROR);
    }
    expect(inputs).toHaveLength(0);
  });

  it("POST /turn rejects an oversized context_packet with 400", async () => {
    const { inputs, post } = recordingHarness();
    const context_packet = { text: "x".repeat(16_385 - JSON.stringify({ text: "" }).length) };
    const response = await post({ context_packet });
    expect(response.status).toBe(400);
    expect(await response.text()).toContain(ERROR);
    expect(inputs).toHaveLength(0);
  });

  it("POST /turn rejects a context_packet nested deeper than 32 with 400", async () => {
    const { inputs, post } = recordingHarness();
    const packetAtDepth = (depth: number): Record<string, unknown> => {
      let value: Record<string, unknown> = { value: 0 };
      for (let level = 1; level < depth; level++) value = { nested: value };
      return value;
    };
    const refused = await post({ context_packet: packetAtDepth(33) });
    expect(refused.status).toBe(400);
    expect(await refused.text()).toContain(ERROR);
    expect(inputs).toHaveLength(0);
    const accepted = await post({ context_packet: packetAtDepth(32) });
    expect(accepted.status).toBe(200);
    await accepted.text();
    expect(inputs).toHaveLength(1);
    const arrays = await post({ context_packet: { nested: Array.from({ length: 1 }, () => {
      let value: unknown = 0;
      for (let level = 0; level < 32; level++) value = [value];
      return value;
    }) } });
    expect(arrays.status).toBe(400);
    expect(await arrays.text()).toContain(ERROR);
    expect(inputs).toHaveLength(1);
  });
});
