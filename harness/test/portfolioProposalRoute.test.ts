import { request } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createHarness } from "../src/server.js";
import {
  PortfolioProposalError,
  type PortfolioProposalErrorCode,
  type PortfolioProposalRequest,
  type PortfolioProposalResult,
  type PortfolioProposalService,
} from "../src/agent/portfolioProposal.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";

const ROUTE = "/internal/portfolio/proposals";
const SECRET = "portfolio-route-test-secret";
const HEADERS = { "x-harness-secret": SECRET, "x-tenant-id": "tenant-proposal" };
const BODY: PortfolioProposalRequest = {
  tenant_id: "tenant-proposal",
  source: { project_id: "portfolio-1", revision: 7, digest64hex: "a".repeat(64) },
  instruction: "Shorten the introduction",
  fields: [{ id: "intro", value: "A thoughtful introduction", max_length: 100 }],
};
const RESULT: PortfolioProposalResult = {
  source: BODY.source,
  model: "contained-model",
  edits: [{ id: "intro", value: "<b>Introduction</b>" }],
  usage: { input_tokens: 5, output_tokens: 3, cost_tokens: 8 },
};
type Service = Pick<PortfolioProposalService, "propose">;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

describe("contained portfolio proposal HTTP route", () => {
  const servers: Server[] = [];
  afterEach(async () => {
    await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
      server.close(error => error ? reject(error) : resolve());
      server.closeAllConnections();
    })));
  });

  function boot(service?: Service, enabled = true, secret = SECRET) {
    const server = createHarness({
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(process.cwd()),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    }, { auth: { enabled, secret }, portfolioProposals: service }).listen(0);
    servers.push(server);
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}${ROUTE}`;
    return { server, url };
  }

  function post(url: string, body: string | Buffer = JSON.stringify(BODY), headers: Record<string, string> = HEADERS) {
    return new Promise<{ status: number; body: unknown; headers: IncomingMessage["headers"] }>((resolve, reject) => {
      const client = request(url, { method: "POST", headers: {
        "content-type": "application/json",
        ...(!headers["content-length"] ? { "transfer-encoding": "chunked" } : {}),
        ...headers,
      } }, res => {
        const chunks: Buffer[] = [];
        res.on("data", chunk => chunks.push(chunk));
        res.on("error", reject);
        res.on("end", () => {
          try {
            resolve({ status: res.statusCode!, body: JSON.parse(Buffer.concat(chunks).toString("utf8")), headers: res.headers });
          } catch (error) { reject(error); }
        });
      });
      client.on("error", reject);
      client.end(body);
    });
  }

  it.each([true, false])("requires the shared secret before reading a body with generic auth enabled=%s", async enabled => {
    const propose = vi.fn(async () => RESULT);
    const { url } = boot({ propose }, enabled);
    for (const secret of [undefined, "wrong-secret"]) {
      // Leave the declared body unsent: a response proves authorization precedes body reads.
      const response = await new Promise<{ status: number; cache: string | undefined }>((resolve, reject) => {
        const client = request(url, {
          method: "POST",
          headers: { "content-length": "1000", ...(secret ? { "x-harness-secret": secret } : {}) },
        }, res => {
          res.resume();
          res.on("end", () => {
            resolve({ status: res.statusCode!, cache: res.headers["cache-control"] });
            client.destroy();
          });
        });
        client.on("error", reject);
        client.flushHeaders();
      });
      expect(response).toEqual({ status: 401, cache: "private, no-store" });
    }
    expect(propose).not.toHaveBeenCalled();
  });

  it("fails closed without a configured shared secret even when generic auth is disabled", async () => {
    const propose = vi.fn(async () => RESULT);
    const { url } = boot({ propose }, false, "");
    expect((await post(url)).status).toBe(401);
    expect(propose).not.toHaveBeenCalled();
  });

  it("requires an explicit tenant header exactly matching the body tenant", async () => {
    const propose = vi.fn(async () => RESULT);
    const { url } = boot({ propose });
    for (const tenant of [undefined, "", "other-tenant"]) {
      const response = await post(url, JSON.stringify(BODY), {
        "x-harness-secret": SECRET, ...(tenant === undefined ? {} : { "x-tenant-id": tenant }),
      });
      expect(response.status).toBe(400);
      expect(response.body).toEqual({ error: { code: "invalid_request" } });
    }
    for (const tenant_id of [undefined, "other-tenant", " tenant-proposal "]) {
      expect((await post(url, JSON.stringify({ ...BODY, tenant_id }))).status).toBe(400);
    }
    expect(propose).not.toHaveBeenCalled();
  });

  it("rejects malformed, non-object, duplicate-key, invalid UTF-8 and oversized JSON", async () => {
    const propose = vi.fn(async () => RESULT);
    const { url } = boot({ propose });
    const encoded = JSON.stringify(BODY);
    const invalid = [
      "{instruction: private payload", "null", "[]",
      encoded.replace('"revision":7', '"revision":7,"revision":8'),
      encoded.replace('"revision":7', '"revision":7,"revis\\u0069on":8'),
      encoded.replace('"tenant_id":', '"tenant_id":"attacker","tenant_id":'),
      Buffer.concat([Buffer.from(encoded.slice(0, -1) + ',"bad":"'), Buffer.from([0xff]), Buffer.from('"}')]),
      JSON.stringify({ ...BODY, instruction: "x".repeat(256 * 1024) }),
    ];
    for (const body of invalid) {
      const response = await post(url, body);
      expect(response.status).toBe(400);
      expect(response.body).toEqual({ error: { code: "invalid_request" } });
      expect(response.headers["cache-control"]).toBe("private, no-store");
    }
    // Also cover the reader's declared-size path, in addition to chunked uploads above.
    const large = invalid[invalid.length - 1] as string;
    expect((await post(url, large, { ...HEADERS, "content-length": String(Buffer.byteLength(large)) })).status).toBe(400);
    expect(propose).not.toHaveBeenCalled();
  });

  it("forwards the exact request and returns the adapter result unchanged", async () => {
    const propose = vi.fn(async (_body: PortfolioProposalRequest, _signal?: AbortSignal) => RESULT);
    const { url } = boot({ propose }, false);
    const response = await post(url);
    expect(response.status).toBe(200);
    expect(response.body).toEqual(RESULT);
    expect(response.headers["content-type"]).toBe("application/json");
    expect(response.headers["cache-control"]).toBe("private, no-store");
    expect(propose).toHaveBeenCalledExactlyOnceWith(BODY, expect.any(AbortSignal));
    expect(propose.mock.calls[0][1]?.aborted).toBe(false);
  });

  it("delegates catalog validation without allowing client context to replace the trusted tenant", async () => {
    const propose = vi.fn(async (body: PortfolioProposalRequest) => {
      expect(body.tenant_id).toBe(BODY.tenant_id);
      throw new PortfolioProposalError("invalid_request");
    });
    const { url } = boot({ propose });
    const body = { ...BODY, fields: [], context: { tenant_id: "attacker" } };
    const response = await post(url, JSON.stringify(body));
    expect(response.status).toBe(400);
    expect(propose.mock.calls[0][0]).toEqual(body);
  });

  it("returns unavailable when no service is injected", async () => {
    const { url } = boot();
    const response = await post(url);
    expect(response.status).toBe(503);
    expect(response.body).toEqual({ error: { code: "unavailable" } });
    expect(response.headers["cache-control"]).toBe("private, no-store");
  });

  it.each<[PortfolioProposalErrorCode, number, string]>([
    ["invalid_request", 400, "invalid_request"], ["invalid_output", 502, "invalid_output"],
    ["grant_unavailable", 503, "unavailable"], ["sdk_failure", 503, "unavailable"],
    ["rate_limit", 429, "rate_limit"],
    ["settlement_failure", 503, "unavailable"], ["timeout", 504, "timeout"], ["aborted", 499, "aborted"],
  ])("maps %s to a fixed sanitized response", async (code, status, publicCode) => {
    const { url } = boot({ async propose() {
      const error = new PortfolioProposalError(code);
      error.message = "private instruction, grant credential, catalog and SDK text";
      throw error;
    } });
    const response = await post(url);
    expect(response.status).toBe(status);
    expect(response.body).toEqual({ error: { code: publicCode } });
    expect(response.headers["cache-control"]).toBe("private, no-store");
    expect(response.headers["retry-after"]).toBeUndefined();
  });

  it("sanitizes unexpected failures without trusting error-shaped objects", async () => {
    for (const error of [new Error("private SDK text"), { code: "invalid_request", message: SECRET }]) {
      const { url } = boot({ async propose() { throw error; } });
      const response = await post(url);
      expect(response.status).toBe(500);
      expect(response.body).toEqual({ error: { code: "internal_error" } });
    }
  });

  it("does not abort on normal request completion and removes its listeners after responding", async () => {
    const started = deferred<AbortSignal>();
    const release = deferred<void>();
    const { server, url } = boot({ async propose(_body, signal) {
      started.resolve(signal!);
      await release.promise;
      return RESULT;
    } });
    let incoming: IncomingMessage | undefined;
    let outgoing: ServerResponse | undefined;
    server.once("request", (req, res) => { incoming = req; outgoing = res; });
    const response = post(url);
    const signal = await started.promise;
    try {
      // Allow node's normal IncomingMessage 'close' event to fire while work is pending.
      await new Promise(resolve => setTimeout(resolve, 25));
      expect(incoming?.complete).toBe(true);
      expect(signal.aborted).toBe(false);
    } finally {
      release.resolve();
    }
    expect((await response).status).toBe(200);
    expect(signal.aborted).toBe(false);
    expect(incoming?.listenerCount("aborted")).toBe(0);
    expect(outgoing?.listenerCount("close")).toBe(0);
  });

  it("aborts in-flight adapter work on a real client disconnect and cleans up listeners", async () => {
    const started = deferred<AbortSignal>();
    const cancelled = deferred<void>();
    const finished = deferred<void>();
    const { server, url } = boot({ async propose(_body, signal) {
      started.resolve(signal!);
      await new Promise<void>(resolve => signal!.addEventListener("abort", () => {
        cancelled.resolve();
        resolve();
      }, { once: true }));
      finished.resolve();
      throw new PortfolioProposalError("aborted");
    } });
    let incoming: IncomingMessage | undefined;
    let outgoing: ServerResponse | undefined;
    server.once("request", (req, res) => { incoming = req; outgoing = res; });
    const client = request(url, { method: "POST", headers: HEADERS });
    client.on("error", () => {});
    client.end(JSON.stringify(BODY));
    try {
      const signal = await started.promise;
      expect(signal.aborted).toBe(false);
      client.destroy();
      await cancelled.promise;
      await finished.promise;
      await new Promise(resolve => setImmediate(resolve));
      expect(signal.aborted).toBe(true);
      expect(incoming?.listenerCount("aborted")).toBe(0);
      expect(outgoing?.listenerCount("close")).toBe(0);
    } finally {
      client.destroy();
    }
  });
});
