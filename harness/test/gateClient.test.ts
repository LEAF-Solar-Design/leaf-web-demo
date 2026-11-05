import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpGateClient } from "../src/ports/impl/gateClient.js";

describe("HttpGateClient transport diagnostics", () => {
  const privateData = "SENTINEL_PRIVATE_GATE_DATA";
  const ctx = {
    tenantId: `${privateData}-tenant`,
    sessionId: `${privateData}-session`,
    turnId: `${privateData}-turn`,
    authoritySessionId: `${privateData}-authority-session`,
    authorityTurnId: `${privateData}-authority-turn`,
  };
  const unreachable = { decision: "deny", reason: "gate_unreachable" };

  function client(fetchImpl: typeof fetch, timeoutMs = 100) {
    return new HttpGateClient({
      appBaseUrl: `https://${privateData}.invalid`,
      dispatchSecret: `${privateData}-secret`,
      timeoutMs,
      fetchImpl,
    });
  }

  function expectDiagnostic(exceptionClass: string, networkCode: string, deadlineMs = 100) {
    const calls = vi.mocked(console.error).mock.calls;
    expect(calls).toHaveLength(1);
    expect(calls[0]).toHaveLength(1);
    const message = calls[0][0];
    expect(typeof message).toBe("string");
    expect(message).not.toMatch(/[\r\n]/);
    const event = JSON.parse(message as string) as Record<string, unknown>;
    expect(Object.keys(event).sort()).toEqual([
      "deadlineMs", "elapsedMs", "event", "exceptionClass", "networkCode",
    ]);
    expect(event).toEqual({
      event: "gate_transport_failure",
      exceptionClass,
      networkCode,
      elapsedMs: expect.any(Number),
      deadlineMs,
    });
    expect(Number.isFinite(event.elapsedMs)).toBe(true);
    expect(event.elapsedMs).toBeGreaterThanOrEqual(0);
    expect(JSON.stringify(calls)).not.toContain(privateData);
  }

  beforeEach(() => {
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("reports a deadline timeout and keeps the unreachable denial", async () => {
    const gate = client((async (_url, init) => {
      const signal = init!.signal!;
      return await new Promise<Response>((_resolve, reject) => {
        if (signal.aborted) reject(signal.reason);
        else signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }) as typeof fetch, 5);

    expect(await gate.check("read_source", { path: privateData }, ctx)).toEqual(unreachable);
    expectDiagnostic("TimeoutError", "unknown", 5);
  });

  it.each(["ECONNREFUSED", "ENOTFOUND", "ERR_TLS_CERT_ALTNAME_INVALID"])(
    "reports an allowlisted nested network code %s without error details",
    async (code) => {
      const error = Object.assign(new TypeError(`${privateData}-message`), {
        cause: Object.assign(new Error(`${privateData}-cause`), { code }),
        request: privateData,
      });
      const gate = client((async () => { throw error; }) as typeof fetch);

      expect(await gate.check("read_source", { path: privateData }, ctx)).toEqual(unreachable);
      expectDiagnostic("TypeError", code);
    },
  );

  it("reports an allowlisted direct network code", async () => {
    const error = Object.assign(new Error(privateData), { code: "ECONNRESET" });
    const gate = client((async () => { throw error; }) as typeof fetch);

    expect(await gate.check("read_source", {}, ctx)).toEqual(unreachable);
    expectDiagnostic("Error", "ECONNRESET");
  });

  it("maps private names and codes to unknown without stringifying the error", async () => {
    const toString = vi.fn(() => privateData);
    const error = {
      name: `${privateData}-name`,
      code: `${privateData}-code`,
      cause: { code: `${privateData}-cause-code` },
      message: `${privateData}-message`,
      stack: `${privateData}-stack`,
      toString,
    };
    const gate = client((async () => { throw error; }) as typeof fetch);

    expect(await gate.check("read_source", { path: privateData }, ctx)).toEqual(unreachable);
    expectDiagnostic("unknown", "unknown");
    expect(toString).not.toHaveBeenCalled();
  });

  it("contains exception property getter failures", async () => {
    const error = Object.defineProperties({}, Object.fromEntries(
      ["name", "code", "cause"].map((key) => [key, {
        get() { throw new Error(privateData); },
      }]),
    ));
    const gate = client((async () => { throw error; }) as typeof fetch);

    expect(await gate.check("read_source", {}, ctx)).toEqual(unreachable);
    expectDiagnostic("unknown", "unknown");
  });

  it("keeps the unreachable denial if the console logger throws", async () => {
    vi.mocked(console.error).mockImplementation(() => { throw new Error(privateData); });
    const gate = client((async () => { throw new Error(privateData); }) as typeof fetch);

    expect(await gate.check("read_source", {}, ctx)).toEqual(unreachable);
    expectDiagnostic("Error", "unknown");
  });

  it.each([
    { status: 503, body: '{"decision":"allow"}', result: { decision: "deny", reason: "gate_error_http_503" } },
    { status: 200, body: "invalid json", result: { decision: "deny", reason: "gate_malformed_response" } },
    { status: 200, body: '{"decision":"invalid"}', result: { decision: "deny", reason: "gate_malformed_response" } },
    { status: 200, body: '{"decision":"allow","policy":"p","rung":3}', result: { decision: "allow", policy: "p", rung: "3" } },
    { status: 200, body: '{"decision":"deny","reason":"policy_denial"}', result: { decision: "deny", reason: "policy_denial" } },
    { status: 200, body: '{"decision":"awaiting_approval","confirmation_id":"c"}', result: { decision: "awaiting_approval", confirmation_id: "c" } },
  ])("preserves response behavior without transport diagnostics: $body ($status)", async ({ status, body, result }) => {
    const gate = client((async () => new Response(body, { status })) as typeof fetch);

    expect(await gate.check("read_source", {}, ctx)).toEqual(result);
    expect(console.error).not.toHaveBeenCalled();
  });

  it("preserves setup failure denial without labeling it a transport failure", async () => {
    const fetchImpl = vi.fn();
    const gate = client(fetchImpl as typeof fetch);
    const args: Record<string, unknown> = {};
    args.circular = args;

    expect(await gate.check("read_source", args, ctx)).toEqual(unreachable);
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(console.error).not.toHaveBeenCalled();
  });
});

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
