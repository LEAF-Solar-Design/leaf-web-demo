import { afterEach, describe, expect, it } from "vitest";

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
    const client = new BrokerApsClientHttp({
      fetchImpl: async (_url, init) => {
        headers = init?.headers;
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      },
    });
    await client.runTool({
      tenantId: "tenant",
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
    expect(headers).toMatchObject({ "X-Broker-Secret": "broker-secret" });
  });
});
