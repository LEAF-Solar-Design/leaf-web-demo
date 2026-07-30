/**
 * POST /turn — converse-turn route (H4, sessions wire leaf-backend-gaps.md §2.1).
 *
 * Hermetic: NO real Agent SDK, NO network beyond localhost. Wires an INLINE scripted
 * ConverseRunner per test (does NOT import a fake from another lane's H3 work, which
 * may not exist yet) against a real `createHarness(...).listen(0)` HTTP server.
 *
 * Proves:
 *   - chunk-by-chunk NDJSON arrival: the first event is observable on the wire before
 *     the stream ends (not buffered until completion);
 *   - the F5 auth gate covers /turn like every other non-health route (401, missing
 *     secret);
 *   - no `converseRunner` wired -> 501 {error:{code:'not_implemented'}};
 *   - a structurally invalid body -> 400 (missing id field; neither text nor confirm);
 *   - a runner that throws mid-stream still ends the (already-200) stream with an
 *     in-band `error` event followed by `turn_complete{stop_reason:'error'}`;
 *   - a runner that rejects on/before the FIRST event (GrantRequiredError) never opens
 *     the stream at all — a clean non-stream 401 {grant_required:true}, mirroring the
 *     /author 401 shape;
 *   - a client abort (req 'close') stops the iterator (the runner's `finally` cleanup
 *     observably runs).
 */

import { connect } from "node:net";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessAuthConfig } from "../src/server.js";
import type {
  ConverseRunner,
  ConverseRunOptions,
  ConverseTurnInput,
  HarnessPorts,
  HarnessTurnEvent,
} from "../src/ports/index.js";
import { GrantPoolUnavailableError, GrantRequiredError } from "../src/ports/impl/oauthGrantProvider.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntil(fn: () => boolean, timeoutMs = 1500, stepMs = 20): Promise<void> {
  const start = Date.now();
  while (!fn()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitUntil timed out");
    await delay(stepMs);
  }
}

/** The four boilerplate ports every harness needs, plus an optional converseRunner. */
function basePorts(converseRunner?: ConverseRunner): HarnessPorts {
  return {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo: new FakeTenantRepoProvider(FIXTURE),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
    ...(converseRunner ? { converseRunner } : {}),
  };
}

function listen(ports: HarnessPorts, auth?: HarnessAuthConfig): { server: Server; baseUrl: string } {
  const server = createHarness(ports, auth ? { auth } : undefined).listen(0);
  const addr = server.address() as AddressInfo;
  return { server, baseUrl: `http://127.0.0.1:${addr.port}` };
}

function validBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    tenant_id: "acme",
    session_id: "sess-1",
    turn_id: "turn-1",
    drawing_id: "dwg-1",
    messages: [],
    text: "count entities per layer",
    ...overrides,
  };
}

function postTurn(
  baseUrl: string,
  body: Record<string, unknown>,
  headers: Record<string, string> = {},
): Promise<Response> {
  return fetch(`${baseUrl}/turn`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

let server: Server | undefined;
afterEach(() => {
  server?.close();
  server = undefined;
});

// --------------------------------------------------------------------------- //
// Chunk-by-chunk NDJSON arrival.
// --------------------------------------------------------------------------- //

describe("POST /turn - streaming", () => {
  it("streams events chunk-by-chunk: the first event arrives well before the stream ends", async () => {
    const chunkedRunner: ConverseRunner = {
      async *runTurn(_input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
        yield { type: "text_delta", data: { text: "hello" } };
        await delay(150); // a real gap: proves the first write wasn't buffered with the rest
        yield { type: "text_delta", data: { text: " world" } };
        yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      },
    };
    const { server: s, baseUrl } = listen(basePorts(chunkedRunner));
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toContain("application/x-ndjson");
    expect(res.body).not.toBeNull();

    const reader = res.body!.getReader();
    const decoder = new TextDecoder();

    const t0 = Date.now();
    const first = await reader.read();
    const tFirst = Date.now();
    expect(first.done).toBe(false);
    const firstChunk = decoder.decode(first.value ?? new Uint8Array());
    expect(JSON.parse(firstChunk.trim())).toMatchObject({ type: "text_delta", data: { text: "hello" } });

    let rest = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      rest += decoder.decode(value);
    }
    const tEnd = Date.now();

    // The first chunk arrived well before the stream finished (it did NOT wait for the
    // 150ms inter-event gap), and later chunks arrived only after that gap.
    expect(tFirst - t0).toBeLessThan(100);
    expect(tEnd - tFirst).toBeGreaterThanOrEqual(100);

    const lines = rest
      .trim()
      .split("\n")
      .filter((l) => l.length > 0)
      .map((l) => JSON.parse(l) as HarnessTurnEvent);
    expect(lines.map((l) => l.type)).toEqual(["text_delta", "turn_complete"]);
    expect(lines[lines.length - 1]).toMatchObject({ type: "turn_complete", data: { stop_reason: "end_turn" } });
  });
});

// --------------------------------------------------------------------------- //
// F5 auth gate applies to /turn like every other non-health route.
// --------------------------------------------------------------------------- //

describe("POST /turn - auth gate", () => {
  it("gate ON + missing secret -> 401 harness_auth_required (never opens the stream)", async () => {
    const neverCalled: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        throw new Error("must not be reached: auth gate should reject before routing");
      },
    };
    const { server: s, baseUrl } = listen(basePorts(neverCalled), {
      enabled: true,
      secret: "shh-do-not-log-this",
    });
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(401);
    expect(res.headers.get("content-type")).not.toContain("ndjson");
    const body = (await res.json()) as { error?: { code?: string } };
    expect(body.error?.code).toBe("harness_auth_required");
  });

  it("gate ON + correct secret -> reaches the runner (200 stream)", async () => {
    const simple: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      },
    };
    const { server: s, baseUrl } = listen(basePorts(simple), { enabled: true, secret: "correct-horse" });
    server = s;

    const res = await postTurn(baseUrl, validBody(), { "x-harness-secret": "correct-horse" });
    expect(res.status).toBe(200);
  });
});

// --------------------------------------------------------------------------- //
// No converseRunner wired -> 501.
// --------------------------------------------------------------------------- //

describe("POST /turn - no runner wired", () => {
  it("501 {error:{code:'not_implemented'}}", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(501);
    const body = (await res.json()) as { error?: { code?: string; message?: string } };
    expect(body.error?.code).toBe("not_implemented");
    expect(typeof body.error?.message).toBe("string");
  });
});

// --------------------------------------------------------------------------- //
// Bad body -> 400 (checked BEFORE the runner is ever touched).
// --------------------------------------------------------------------------- //

describe("POST /turn - bad body", () => {
  it("missing a required id field -> 400", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;
    const { tenant_id: _drop, ...rest } = validBody();
    const res = await postTurn(baseUrl, rest);
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error?: { message?: string } };
    expect(typeof body.error?.message).toBe("string");
  });

  it("neither text, image, nor confirm present -> 400", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;
    const { text: _drop, ...rest } = validBody();
    const res = await postTurn(baseUrl, rest);
    expect(res.status).toBe(400);
  });

  it("accepts image-only turns and rejects spoofed media bytes before the runner", async () => {
    let seen: ConverseTurnInput | undefined;
    const capturing: ConverseRunner = {
      async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
        seen = input;
        yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      },
    };
    const { server: s, baseUrl } = listen(basePorts(capturing));
    server = s;
    const { text: _text, ...imageOnly } = validBody({
      images: [{ media_type: "image/png", data: "iVBORw0KGgo=" }],
    });
    const accepted = await postTurn(baseUrl, imageOnly);
    expect(accepted.status).toBe(200);
    await accepted.text();
    expect(seen?.images).toEqual([{ media_type: "image/png", data: "iVBORw0KGgo=" }]);

    const spoofed = await postTurn(baseUrl, validBody({
      images: [{ media_type: "image/jpeg", data: "iVBORw0KGgo=" }],
    }));
    expect(spoofed.status).toBe(400);
    const body = (await spoofed.json()) as { error: { message: string } };
    expect(body.error.message).toContain("does not match");
  });

  it("invalid JSON body -> 400", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;
    const res = await fetch(`${baseUrl}/turn`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{not json",
    });
    expect(res.status).toBe(400);
  });
});

// --------------------------------------------------------------------------- //
// Mount-your-LLM validation: an unknown model or malformed credential_grant is
// a 400 BEFORE the runner is touched (defense in depth over the app's own gate).
// --------------------------------------------------------------------------- //

describe("POST /turn - mount your LLM validation", () => {
  it("unknown model -> 400 (never reaches the runner)", async () => {
    const neverCalled: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        throw new Error("must not be reached: invalid model should reject first");
      },
    };
    const { server: s, baseUrl } = listen(basePorts(neverCalled));
    server = s;
    const res = await postTurn(baseUrl, validBody({ model: "gpt-4o" }));
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error?: { message?: string } };
    expect(String(body.error?.message)).toContain("model must be one of");
  });

  it("malformed credential_grant -> 400", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;
    const res = await postTurn(baseUrl, validBody({ credential_grant: { kind: "api_key" } }));
    expect(res.status).toBe(400);
    const body = (await res.json()) as { error?: { message?: string } };
    expect(String(body.error?.message)).toContain("credential_grant");
  });

  it("a valid model + credential_grant pass validation and reach the runner", async () => {
    let seen: ConverseTurnInput | undefined;
    const capturing: ConverseRunner = {
      async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
        seen = input;
        yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      },
    };
    const { server: s, baseUrl } = listen(basePorts(capturing));
    server = s;
    const res = await postTurn(
      baseUrl,
      validBody({
        model: "claude-opus-4-8",
        credential_grant: { kind: "oauth", oauth_token: "sk-ant-oat-xyz" },
      }),
    );
    expect(res.status).toBe(200);
    await res.text();
    expect(seen?.model).toBe("claude-opus-4-8");
    expect(seen?.credential_grant).toEqual({ kind: "oauth", oauth_token: "sk-ant-oat-xyz" });
  });
});

// --------------------------------------------------------------------------- //
// Mid-stream throw -> in-band error + turn_complete(stop_reason:'error').
// --------------------------------------------------------------------------- //

describe("POST /turn - mid-stream throw", () => {
  it("runner throws after yielding -> still 200, ends with error + turn_complete lines", async () => {
    const throwing: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        yield { type: "text_delta", data: { text: "partial" } };
        throw new Error("boom-mid-stream");
      },
    };
    const { server: s, baseUrl } = listen(basePorts(throwing));
    server = s;

    const res = await postTurn(baseUrl, validBody());
    // Headers were already committed on the first (successful) yield.
    expect(res.status).toBe(200);
    const text = await res.text();
    const lines = text
      .trim()
      .split("\n")
      .filter((l) => l.length > 0)
      .map((l) => JSON.parse(l) as HarnessTurnEvent);

    expect(lines[0]).toMatchObject({ type: "text_delta", data: { text: "partial" } });
    const errorLine = lines.find((l) => l.type === "error");
    expect(errorLine).toBeDefined();
    expect(String((errorLine!.data as { message?: string }).message)).toContain("boom-mid-stream");

    const last = lines[lines.length - 1];
    expect(last).toMatchObject({ type: "turn_complete", data: { stop_reason: "error" } });
  });
});

// --------------------------------------------------------------------------- //
// A rejection on/before the FIRST event -> non-stream 401 (GrantRequiredError).
// --------------------------------------------------------------------------- //

describe("POST /turn - grant error resolved before streaming", () => {
  it("an exhausted mounted pool returns a clean immediate quota response", async () => {
    const runner: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        throw new GrantPoolUnavailableError(420);
      },
    };
    const { server: s, baseUrl } = listen(basePorts(runner));
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(429);
    expect(await res.json()).toMatchObject({
      errorCode: "llm_quota_exhausted",
      retry_after_s: 420,
    });
  });

  it("runner throws GrantRequiredError synchronously -> 401 {grant_required:true}, no stream opened", async () => {
    const grantless: ConverseRunner = {
      runTurn(): AsyncIterable<HarnessTurnEvent> {
        throw new GrantRequiredError("acme");
      },
    };
    const { server: s, baseUrl } = listen(basePorts(grantless));
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(401);
    expect(res.headers.get("content-type")).toContain("application/json");
    const body = (await res.json()) as { grant_required?: boolean; error?: { code?: string } };
    expect(body.grant_required).toBe(true);
    expect(body.error?.code).toBe("grant_required");
  });

  it("runner rejects GrantRequiredError on the FIRST yield (async generator) -> 401 {grant_required:true}", async () => {
    // A generator function's body does not run until the first .next() call, so this
    // exercises the "throws on first pull" path distinctly from the synchronous-throw
    // test above (where runTurn() itself throws before returning an iterable at all).
    const grantlessAsync: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        throw new GrantRequiredError("acme");
      },
    };
    const { server: s, baseUrl } = listen(basePorts(grantlessAsync));
    server = s;

    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(401);
    const body = (await res.json()) as { grant_required?: boolean };
    expect(body.grant_required).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// Client abort -> req 'close' aborts the iterator.
// --------------------------------------------------------------------------- //

describe("POST /turn - client abort", () => {
  it("aborting the request stops the iterator (runner's finally cleanup runs)", async () => {
    let cleanedUp = false;
    const longRunner: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        try {
          yield { type: "text_delta", data: { text: "one" } };
          await delay(200);
          yield { type: "text_delta", data: { text: "two" } };
          yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
        } finally {
          cleanedUp = true;
        }
      },
    };
    const { server: s, baseUrl } = listen(basePorts(longRunner));
    server = s;

    const controller = new AbortController();
    const res = await fetch(`${baseUrl}/turn`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(validBody()),
      signal: controller.signal,
    });
    const reader = res.body!.getReader();
    await reader.read(); // observe the first event, then abort mid-stream
    controller.abort();

    await waitUntil(() => cleanedUp, 2000);
    expect(cleanedUp).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// FINDING 5: the close listener must be installed on the RESPONSE socket, and
// BEFORE the first pull - a disconnect that happens WHILE the runner's first event
// is still pending (no response bytes sent yet, res.writeHead() not even called)
// must still terminate the runner's iteration. The pre-fix code attached its close
// listener only AFTER that first pull resolved, on `req` (already fully drained by
// readJsonBody() by that point) - an event emitted before a listener is attached is
// simply dropped, so this exact window was silently missed.
// --------------------------------------------------------------------------- //

describe("POST /turn - client disconnect WHILE the first event is still pending", () => {
  it("destroying the raw socket before any bytes are written still terminates the runner promptly (finally cleanup runs)", async () => {
    let terminated = false;
    // NOTE on timing: an async generator's OWN internal `await` (the delay(150) below)
    // cannot be preempted by an externally-queued `.return()` - per the spec, that
    // request only takes effect once the generator reaches its next yield/return. So
    // the fixed code still lets the FIRST event through the pending pull at ~150ms; the
    // observable difference is what happens next. With the fix, the close listener
    // (attached BEFORE the first pull, on `res`) already queued `iterator.return()` by
    // the time the first yield lands, so the SDK-loop equivalent unwinds through
    // `finally` right away instead of continuing into the long `delay(2000)` below - a
    // real (non-test-double) runner honoring the same iterator protocol would likewise
    // stop pulling instead of spending further LLM turns after the client is gone. The
    // pre-fix code misses the close event entirely (listener attached post-pull, on the
    // already-drained `req`), so the generator runs to natural completion at ~2150ms.
    const slowRunner: ConverseRunner = {
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> {
        try {
          // Nothing has gone out on the wire yet, and res.writeHead() has not even been
          // called, while this first pull is pending.
          await delay(150);
          yield { type: "text_delta", data: { text: "first" } };
          await delay(2000);
          yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
        } finally {
          terminated = true;
        }
      },
    };
    const { server: s } = listen(basePorts(slowRunner));
    server = s;
    const addr = s.address() as AddressInfo;

    const body = JSON.stringify(validBody());
    const rawRequest =
      `POST /turn HTTP/1.1\r\n` +
      `Host: 127.0.0.1:${addr.port}\r\n` +
      `Content-Type: application/json\r\n` +
      `Content-Length: ${Buffer.byteLength(body)}\r\n` +
      `Connection: close\r\n` +
      `\r\n${body}`;

    const socket = connect(addr.port, "127.0.0.1");
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", () => resolve());
      socket.once("error", reject);
    });
    socket.on("error", () => {}); // destroying our own end can surface ECONNRESET locally
    socket.write(rawRequest);

    // Destroy well before the runner's simulated 150ms first-pull delay elapses -
    // exactly the pre-writeHead window the old post-first-pull `req` listener missed.
    await delay(30);
    socket.destroy();

    // Comfortably above the ~150ms first-pull settle time, comfortably below the
    // ~2150ms it would take the pre-fix code's generator to finish on its own.
    await waitUntil(() => terminated, 1200);
    expect(terminated).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// Round-2 review blocker: `iterator.return()` alone QUEUES behind a pending
// `next()` (async-generator spec) — a first pull that never settles on its own
// (a hung/expensive SDK call) would keep running after the client is gone. The
// fix threads an AbortSignal into runTurn (ConverseRunOptions.signal) and fires
// it in the close handler, preempting the pending pull instead of waiting it out.
// --------------------------------------------------------------------------- //

describe("POST /turn - client disconnect ABORTS a hung first pull via the runner's AbortSignal", () => {
  it("the signal handed to runTurn fires on disconnect while the first pull is still pending", async () => {
    let sawSignal = false;
    let abortedPromptly = false;
    const hungRunner: ConverseRunner = {
      async *runTurn(_input: ConverseTurnInput, opts?: ConverseRunOptions): AsyncGenerator<HarnessTurnEvent> {
        // Simulates an SDK first call that NEVER settles on its own. Without the
        // signal threaded through (the pre-fix code), nothing can resolve this
        // promise: the generator hangs, `abortedPromptly` never flips, and the
        // waitUntil below times the test out — i.e. this test genuinely gates
        // the fix, it cannot pass by iterator.return() semantics alone.
        const sig = opts?.signal;
        sawSignal = sig instanceof AbortSignal;
        await new Promise<void>((resolve) => {
          if (!sig) return; // no signal -> hang forever (regression -> timeout)
          if (sig.aborted) return resolve();
          sig.addEventListener("abort", () => resolve(), { once: true });
        });
        abortedPromptly = true;
        // A real SDK runner's query would throw an AbortError here; ending the
        // generator without further yields exercises the same server-side path
        // (nothing further is written to the closed response).
      },
    };
    const { server: s } = listen(basePorts(hungRunner));
    server = s;
    const addr = s.address() as AddressInfo;

    const body = JSON.stringify(validBody());
    const rawRequest =
      `POST /turn HTTP/1.1\r\n` +
      `Host: 127.0.0.1:${addr.port}\r\n` +
      `Content-Type: application/json\r\n` +
      `Content-Length: ${Buffer.byteLength(body)}\r\n` +
      `Connection: close\r\n` +
      `\r\n${body}`;

    const socket = connect(addr.port, "127.0.0.1");
    await new Promise<void>((resolve, reject) => {
      socket.once("connect", () => resolve());
      socket.once("error", reject);
    });
    socket.on("error", () => {});
    socket.write(rawRequest);

    // Give the request time to reach streamTurn (first pull now pending), then vanish.
    await delay(60);
    socket.destroy();

    await waitUntil(() => abortedPromptly, 1500);
    expect(sawSignal).toBe(true);
    expect(abortedPromptly).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// Request body ceilings, and the one-of rule at the boundary.
// --------------------------------------------------------------------------- //

/**
 * Send a CHUNKED request (no content-length, so the ceiling can only be
 * enforced as bytes arrive) and return whatever the server actually wrote back.
 * Raw sockets rather than fetch: the claim under test is about the bytes on the
 * wire — that the client reads a 413 envelope instead of a connection reset.
 */
function rawChunkedPost(
  port: number,
  path: string,
  totalBytes: number,
): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, "127.0.0.1");
    const chunk = "x".repeat(64 * 1024);
    const chunkFrame = `${chunk.length.toString(16)}\r\n${chunk}\r\n`;
    let response = "";
    let sent = 0;

    socket.on("data", (d: Buffer) => { response += d.toString("utf8"); });
    socket.on("close", () => resolve(response));
    // Writing into a socket the server has deliberately closed fails; that is
    // the POINT of connection:close, not a test failure. Only a connect-time
    // error is real, and that arrives before anything was written.
    socket.on("error", () => { if (!response) reject(new Error("socket error before any response")); });

    socket.once("connect", () => {
      socket.write(
        `POST ${path} HTTP/1.1\r\n` +
        `Host: 127.0.0.1:${port}\r\n` +
        `Content-Type: application/json\r\n` +
        `Transfer-Encoding: chunked\r\n\r\n`,
      );
      const pump = (): void => {
        while (sent < totalBytes && !socket.destroyed) {
          sent += chunk.length;
          if (!socket.write(chunkFrame)) { socket.once("drain", pump); return; }
        }
        if (!socket.destroyed) socket.write("0\r\n\r\n");
      };
      pump();
    });
  });
}

/**
 * `send()` writes the payload without a content-length, so node frames the
 * response with `transfer-encoding: chunked`. Undo that framing to get at the
 * JSON. Tolerates an unframed body in case the response ever carries a length.
 */
function decodeBody(body: string): string {
  if (body.trimStart().startsWith("{")) return body;
  let out = "";
  let i = 0;
  for (;;) {
    const nl = body.indexOf("\r\n", i);
    if (nl < 0) break;
    const size = Number.parseInt(body.slice(i, nl), 16);
    if (!Number.isFinite(size) || size === 0) break;
    out += body.slice(nl + 2, nl + 2 + size);
    i = nl + 2 + size + 2;
  }
  return out;
}

/**
 * Same, but DECLARING a Content-Length so refusal happens before any byte is
 * read. That branch discards from zero, which is what made a large declared
 * body reset instead of answering.
 */
function rawDeclaredPost(port: number, path: string, totalBytes: number): Promise<string> {
  return new Promise((resolve, reject) => {
    const socket = connect(port, "127.0.0.1");
    const chunk = "x".repeat(64 * 1024);
    let response = "";
    let sent = 0;

    socket.on("data", (d: Buffer) => { response += d.toString("utf8"); });
    socket.on("close", () => resolve(response));
    socket.on("error", () => { if (!response) reject(new Error("socket error before any response")); });

    socket.once("connect", () => {
      socket.write(
        `POST ${path} HTTP/1.1\r\n` +
        `Host: 127.0.0.1:${port}\r\n` +
        `Content-Type: application/json\r\n` +
        `Content-Length: ${totalBytes}\r\n\r\n`,
      );
      const pump = (): void => {
        while (sent < totalBytes && !socket.destroyed) {
          sent += chunk.length;
          if (!socket.write(chunk)) { socket.once("drain", pump); return; }
        }
      };
      pump();
    });
  });
}

describe("POST /turn - request body ceiling", () => {
  it("answers an over-limit CHUNKED body with the 413 envelope, not a connection reset", async () => {
    let runnerCalled = false;
    const runner: ConverseRunner = {
      // eslint-disable-next-line require-yield
      async *runTurn(): AsyncGenerator<HarnessTurnEvent> { runnerCalled = true; },
    };
    const { server: s } = listen(basePorts(runner));
    server = s;
    const addr = s.address() as AddressInfo;

    // 2.1 MB against the 2,000,000-byte /turn ceiling, with no content-length
    // to check up front, so refusal can only happen mid-body.
    const response = await rawChunkedPost(addr.port, "/turn", 2_100_000);

    // The regression this pins: req.destroy() used to tear the socket down
    // before the handler could write, so the client got a reset and an EMPTY
    // response here.
    expect(response).not.toBe("");
    expect(response.split("\r\n")[0]).toContain("413");
    expect(response.toLowerCase()).toContain("connection: close");
    const body = response.slice(response.indexOf("\r\n\r\n") + 4);
    expect(JSON.parse(decodeBody(body))).toMatchObject({
      error: { message: "request body exceeds the harness cap" },
    });
    expect(runnerCalled).toBe(false);
  });

  it("answers an over-limit DECLARED body with the same 413 envelope", async () => {
    const { server: s, baseUrl } = listen(basePorts());
    server = s;

    const res = await fetch(`${baseUrl}/turn`, {
      method: "POST",
      headers: { "content-type": "application/json", "content-length": "3000000" },
      body: "x".repeat(3_000_000),
    });
    expect(res.status).toBe(413);
    expect(res.headers.get("connection")).toBe("close");
    await expect(res.json()).resolves.toMatchObject({
      error: { message: "request body exceeds the harness cap" },
    });
  });

  it("accepts a /turn body that a 1 MiB image plus prior context would produce", async () => {
    const seen: ConverseTurnInput[] = [];
    const runner: ConverseRunner = {
      async *runTurn(input: ConverseTurnInput): AsyncGenerator<HarnessTurnEvent> {
        seen.push(input);
        yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      },
    };
    const { server: s, baseUrl } = listen(basePorts(runner));
    server = s;

    // The worst case the APP can legitimately forward: a message at the app's
    // own 1.5 MB inbound cap, plus the byte-bounded prior context it appends
    // AFTERWARDS. Sizing /turn to 1,500,000 refused exactly this.
    const res = await postTurn(baseUrl, validBody({
      text: "x".repeat(1_500_000 - 2_000),
      messages: [{ role: "user", text: "y".repeat(256 * 1024) }],
    }));
    expect(res.status).toBe(200);
    await res.text();
    expect(seen).toHaveLength(1);
  });
});

describe("POST /turn - a turn is a user message or a confirmation, not both", () => {
  const cases: Array<[string, Record<string, unknown>]> = [
    ["text + confirm", { text: "hi", confirm: { confirmationId: "c1", approved: true } }],
    ["images + confirm", {
      text: undefined,
      images: [{ media_type: "image/png", data: Buffer.concat([
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(16),
      ]).toString("base64") }],
      confirm: { confirmationId: "c1", approved: true },
    }],
  ];

  for (const [label, overrides] of cases) {
    it(`refuses ${label} at the boundary, before the runner is ever reached`, async () => {
      let runnerCalled = false;
      const runner: ConverseRunner = {
        // eslint-disable-next-line require-yield
        async *runTurn(): AsyncGenerator<HarnessTurnEvent> { runnerCalled = true; },
      };
      const { server: s, baseUrl } = listen(basePorts(runner));
      server = s;

      const body = validBody(overrides);
      if (overrides.text === undefined) delete body.text;
      const res = await postTurn(baseUrl, body);

      expect(res.status).toBe(400);
      await expect(res.json()).resolves.toMatchObject({
        error: { message: "a turn carries either text/images or confirm, not both" },
      });
      // The whole point: rejected BEFORE streamTurn, so nothing downstream
      // acquired a grant or wrote a confirmation mirror. Leaving text+confirm
      // to ConverseLoop made this a post-side-effect 500 instead of a 400.
      expect(runnerCalled).toBe(false);
    });
  }
});

// Realistic budget: these push 9-40 MiB through a loopback socket, so the 5s
// default is a statement about the transfer, not about the code under test.
describe("body ceiling - an oversized DECLARED body on an 8 MiB route", { timeout: 60_000 }, () => {
  // Round 8's sharpest case. Refusal on the declared Content-Length happens
  // before the first byte arrives, so counting the drain allowance from the
  // moment of refusal counted the WHOLE body as drain. On an 8 MiB route every
  // oversized body is over 8 MiB, so a 4 MiB allowance was always blown and
  // every one of them was cut off instead of answered. Measuring the allowance
  // from the CAP is what fixes it.
  // `/run-registered` rather than `/author`: with authored execution disabled
  // (the default here) /author answers 403 BEFORE reading the body at all, so
  // it exercises a pre-existing "reply without reading" path instead of the
  // ceiling. That path has the same client-side symptom and is worth its own
  // look, but it is not what this test is about.
  const routes = ["/run-registered"];

  it.each(routes)("answers %s with the 413 envelope, not a reset", async (route) => {
    const { server: s } = listen(basePorts());
    server = s;
    const addr = s.address() as AddressInfo;

    const response = await rawDeclaredPost(addr.port, route, 9 * 1024 * 1024);

    expect(response).not.toBe("");
    expect(response.split("\r\n")[0]).toContain("413");
    expect(response.toLowerCase()).toContain("connection: close");
    const body = response.slice(response.indexOf("\r\n\r\n") + 4);
    expect(JSON.parse(decodeBody(body))).toMatchObject({
      error: { message: "request body exceeds the harness cap" },
    });
  });

  it("stops reading a body past the allowance instead of buffering it", async () => {
    // Beyond cap + allowance the peer is past anything a client legitimately
    // sends, and HTTP cannot promise it reads an early response while it is
    // still uploading. What IS promised: the read is bounded and the server
    // stays healthy. Asserting a delivered envelope here would be asserting
    // something the protocol does not guarantee.
    const { server: s, baseUrl } = listen(basePorts());
    server = s;
    const addr = s.address() as AddressInfo;

    await rawDeclaredPost(addr.port, "/turn", 40 * 1024 * 1024).catch(() => "");

    // The server did not fall over, and the next request is answered normally.
    const res = await postTurn(baseUrl, validBody());
    expect(res.status).toBe(501);   // no converseRunner wired in basePorts()
    await res.text();
  });
});

describe("body ceiling - the connection outlives the upload", { timeout: 60_000 }, () => {
  it("does not answer or close until the oversized body has finished arriving", async () => {
    // The ordering that makes a 413 readable. `connection: close` means node
    // half-closes the socket the moment the response is written, so answering
    // while the peer is still uploading hands it a broken pipe and it reports a
    // network failure instead of the envelope — and the app turns that into
    // BROKER_UNREACHABLE. This asserts the property directly rather than
    // through a client's reaction to it, which is timing-dependent.
    const { server: s } = listen(basePorts());
    server = s;
    const addr = s.address() as AddressInfo;

    const declared = 3_000_000;      // over the 2,000,000 /turn ceiling
    const socket = connect(addr.port, "127.0.0.1");
    let response = "";
    let sawFin = false;
    socket.on("data", (d: Buffer) => { response += d.toString("utf8"); });
    socket.on("end", () => { sawFin = true; });
    socket.on("error", () => { /* only meaningful after the assertions below */ });

    await new Promise<void>((resolve) => socket.once("connect", () => resolve()));
    socket.write(
      `POST /turn HTTP/1.1\r\nHost: 127.0.0.1:${addr.port}\r\n` +
      `Content-Type: application/json\r\nContent-Length: ${declared}\r\n\r\n`,
    );
    socket.write("x".repeat(64 * 1024));      // a first slice, then stall
    await delay(400);

    // Mid-upload: nothing answered, nothing closed. The peer can still write.
    expect(response).toBe("");
    expect(sawFin).toBe(false);
    expect(socket.writable).toBe(true);

    // Finish the body; NOW the answer is safe and must arrive.
    socket.write("x".repeat(declared - 64 * 1024));
    await waitUntil(() => response.includes("\r\n\r\n"), 20_000);
    expect(response.split("\r\n")[0]).toContain("413");
    socket.destroy();
  });
});
