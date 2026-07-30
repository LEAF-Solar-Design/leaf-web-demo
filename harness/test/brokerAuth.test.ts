import { afterEach, describe, expect, it } from "vitest";

import type { BrokerRunRequest } from "../src/ports/index.js";
import { BrokerApsClientHttp } from "../src/ports/impl/brokerApsClient.js";

const ORIGINAL = process.env.LEAF_BROKER_SECRET;

afterEach(() => {
  if (ORIGINAL === undefined) delete process.env.LEAF_BROKER_SECRET;
  else process.env.LEAF_BROKER_SECRET = ORIGINAL;
});

describe("harness broker caller authentication", () => {
  it("trims and sends the broker secret without exposing it in the body", async () => {
    process.env.LEAF_BROKER_SECRET = "  broker-secret\r\n";
    let headers: unknown;
    let body: unknown;
    const client = new BrokerApsClientHttp({
      fetchImpl: async (_url, init) => {
        headers = init?.headers;
        body = JSON.parse(String(init?.body));
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
    await client.runTool({
      tenantId: "tenant",
      ledgerEventKey: "   ",
      tool: {
        name: "read",
        version: "1",
        description: "read",
        kind: "script",
        engine_op: "read",
        params: { type: "object" },
        returns: { type: "object" },
        capabilities: ["drawing.read"],
        provenance: { author: "agent", created: "2026-07-23T00:00:00Z" },
      },
      params: {},
      dwg: "drawing",
      apsLive: false,
      testSource: "def run(intake, params):\n    return ({}, None)\n",
    });
    expect(headers).toMatchObject({ "X-Broker-Secret": "broker-secret" });
    expect(body).toMatchObject({
      tenant_id: "tenant",
      ledger_event_key: expect.stringMatching(/^harness:[0-9a-f-]+$/),
      aps_live: false,
      test_source: "def run(intake, params):\n    return ({}, None)\n",
    });
    expect(JSON.stringify(body)).not.toContain("broker-secret");
  });

  it("generates a distinct broker ledger event key for each call", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    const client = new BrokerApsClientHttp({
      fetchImpl: async (_url, init) => {
        bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
    const request: BrokerRunRequest = {
      tenantId: "tenant",
      tool: {
        name: "read",
        version: "1",
        description: "read",
        kind: "script" as const,
        engine_op: "read",
        params: { type: "object" },
        returns: { type: "object" },
        capabilities: ["drawing.read"],
        provenance: { author: "agent", created: "2026-07-23T00:00:00Z" },
      },
      params: {},
      dwg: "drawing",
      apsLive: false,
    };

    await client.runTool(request);
    await client.runTool(request);

    expect(bodies[0]?.ledger_event_key).toMatch(/^harness:[0-9a-f-]+$/);
    expect(bodies[1]?.ledger_event_key).toMatch(/^harness:[0-9a-f-]+$/);
    expect(bodies[0]?.ledger_event_key).not.toBe(bodies[1]?.ledger_event_key);
  });

  it("normalizes and preserves an explicitly supplied broker ledger event key", async () => {
    let body: Record<string, unknown> = {};
    const client = new BrokerApsClientHttp({
      fetchImpl: async (_url, init) => {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });

    await client.runTool({
      tenantId: "tenant",
      ledgerEventKey: "  author-stage:fixed-key  ",
      tool: {
        name: "read",
        version: "1",
        description: "read",
        kind: "script",
        engine_op: "read",
        params: { type: "object" },
        returns: { type: "object" },
        capabilities: ["drawing.read"],
        provenance: { author: "agent", created: "2026-07-23T00:00:00Z" },
      },
      params: {},
      dwg: "drawing",
      apsLive: false,
    });

    expect(body.ledger_event_key).toBe("author-stage:fixed-key");
  });
});
