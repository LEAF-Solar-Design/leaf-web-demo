import { describe, expect, it } from "vitest";

import { HttpGateClient } from "../src/ports/impl/gateClient.js";

describe("HttpGateClient authority tuple", () => {
  it("sends app authority ids separately from harness-private gate ids", async () => {
    let body: Record<string, unknown> = {};
    const client = new HttpGateClient({
      appBaseUrl: "http://app",
      dispatchSecret: "test-secret",
      fetchImpl: (async (_url: string | URL | Request, init?: RequestInit) => {
        body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return new Response(JSON.stringify({ decision: "allow" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as typeof fetch,
    });

    const result = await client.check("read_platform_state", {}, {
      tenantId: "tenant-pro",
      sessionId: "harness-session",
      turnId: "harness-turn",
      authoritySessionId: "app-session",
      authorityTurnId: "app-turn",
    });

    expect(result.decision).toBe("allow");
    expect(body).toMatchObject({
      tenant_id: "tenant-pro",
      session_id: "harness-session",
      turn_id: "harness-turn",
      authority_session_id: "app-session",
      authority_turn_id: "app-turn",
    });
  });
});
