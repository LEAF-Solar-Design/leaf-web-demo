/*
 * PARKED at the 2026-07-21 merge resolution (spine x sessions-wire): this file
 * tests the §18 /converse/* server surface, which is UNWIRED while the §2.1
 * sessions wire (POST /turn) owns the live turn path. Renamed *.test.parked.ts
 * so vitest skips it; restore the name when spine unification re-wires the
 * routes. The spine's module-level suites (converseLoop, converseSdkRunner,
 * sessionStore, converseRuntimeSeparation) still run.
 */
// @ts-nocheck -- parked: written against the spine's HarnessPorts.converseRunner
// typing, which the sessions wire now owns; un-park + retype at unification.
/**
 * /converse/* HTTP route tests — hermetic (fakes only, zero network egress, zero
 * Anthropic). Proves the wire-contract section-1 surface as served by
 * harness/src/server.ts:
 *
 *   - the EXISTING X-Harness-Secret gate covers every /converse route (401);
 *   - POST /converse/sessions is idempotent per (tenant, drawing);
 *   - POST .../messages -> 202 {turnId} | 409 {error:"turn_in_progress", turnId}
 *     | 401 {error:"grant_required"} (runner factory throwing GrantRequiredError);
 *   - GET .../stream?afterSeq=N replays ONLY seq > N from the store, then
 *     live-tails loop events (section-3 envelopes on both paths);
 *   - GET .../transcript?limit=N returns most recent N, ascending seq;
 *   - tenant mismatch answers 404 {error:"session_not_found"} everywhere
 *     (no cross-tenant existence oracle); DELETE archives;
 *   - unwired converse ports -> 501.
 */

import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessAuthConfig } from "../src/server.js";
import { GrantRequiredError } from "../src/ports/impl/oauthGrantProvider.js";
import type { ConverseEvent, HarnessPorts } from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "s3cr3t-converse-shared-xyz";
const PACKET = { drawing: { id: "rooftop_demo" }, grant: { kind: "oauth", degraded: false } };

const AUTH_OFF: HarnessAuthConfig = { enabled: false, secret: "" };

interface Rig {
  server: Server;
  baseUrl: string;
  runner: FakeConverseRunner;
  store: FakeSessionStore;
  gate: FakeGateClient;
}

const openServers: Server[] = [];
afterEach(() => {
  for (const s of openServers.splice(0)) s.close();
});

function rig(opts?: {
  auth?: HarnessAuthConfig;
  omitConverse?: boolean;
  converseRunnerFor?: (tenantId: string) => Promise<never>;
}): Rig {
  const runner = new FakeConverseRunner();
  const store = new FakeSessionStore();
  const gate = new FakeGateClient();
  const ports: HarnessPorts = {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo: new FakeTenantRepoProvider(FIXTURE),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
    ...(opts?.omitConverse
      ? {}
      : {
          // The shared fake runner is the hermetic runner source; the live path
          // instead builds a fresh ConverseSdkRunner per turn via converseRunnerFor.
          ...(opts?.converseRunnerFor ? {} : { converseRunner: runner }),
          appRun: new FakeAppRunClient(),
          gate,
          sessionStore: store,
        }),
  };
  const server = createHarness(ports, {
    auth: opts?.auth ?? AUTH_OFF,
    ...(opts?.converseRunnerFor ? { converseRunnerFor: opts.converseRunnerFor } : {}),
  }).listen(0);
  openServers.push(server);
  const addr = server.address() as AddressInfo;
  return { server, baseUrl: `http://127.0.0.1:${addr.port}`, runner, store, gate };
}

async function post(
  baseUrl: string,
  path: string,
  body: Record<string, unknown>,
  headers: Record<string, string> = {},
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
  return { status: res.status, json: (await res.json()) as Record<string, unknown> };
}

async function get(
  baseUrl: string,
  path: string,
  headers: Record<string, string> = {},
): Promise<{ status: number; json: Record<string, unknown> }> {
  const res = await fetch(`${baseUrl}${path}`, { headers });
  return { status: res.status, json: (await res.json()) as Record<string, unknown> };
}

async function createSession(r: Rig, tenantId = "t1", drawingId = "d1"): Promise<string> {
  const { status, json } = await post(r.baseUrl, "/converse/sessions", { tenantId, drawingId });
  expect(status).toBe(200);
  return json.sessionId as string;
}

/** POST a text message and await the persisted turn_complete (poll the store). */
async function sendAndFinish(r: Rig, sessionId: string, tenantId: string, text: string): Promise<string> {
  const { status, json } = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
    tenantId,
    text,
    contextPacket: PACKET,
  });
  expect(status).toBe(202);
  const turnId = json.turnId as string;
  expect(turnId).toBeTruthy();
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    const events = await r.store.eventsAfter(sessionId, 0);
    if (events.some((e) => e.type === "turn_complete" && e.turn_id === turnId)) return turnId;
    await new Promise((res) => setTimeout(res, 10));
  }
  throw new Error("turn did not complete within 2s");
}

/** Read SSE events from a stream until `enough` says stop, then abort the tail. */
async function readSse(
  baseUrl: string,
  path: string,
  enough: (events: ConverseEvent[]) => boolean,
  timeoutMs = 3000,
): Promise<ConverseEvent[]> {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  const events: ConverseEvent[] = [];
  try {
    const res = await fetch(`${baseUrl}${path}`, { signal: ac.signal });
    expect(res.status).toBe(200);
    expect(res.headers.get("content-type")).toBe("text/event-stream");
    const reader = res.body!.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (!enough(events)) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const dataLine = frame.split("\n").find((l) => l.startsWith("data: "));
        if (dataLine) events.push(JSON.parse(dataLine.slice(6)) as ConverseEvent);
      }
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") throw e;
  } finally {
    clearTimeout(timer);
    ac.abort();
  }
  return events;
}

// --------------------------------------------------------------------------- //
// Route auth — the EXISTING X-Harness-Secret gate covers /converse/*.
// --------------------------------------------------------------------------- //

describe("/converse routes — X-Harness-Secret gate", () => {
  it("401 without (or with a wrong) secret on every converse route; 200 with it", async () => {
    const r = rig({ auth: { enabled: true, secret: SECRET } });

    const noSecret = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1", drawingId: "d1" });
    expect(noSecret.status).toBe(401);

    const wrong = await post(
      r.baseUrl,
      "/converse/sessions",
      { tenantId: "t1", drawingId: "d1" },
      { "x-harness-secret": "nope" },
    );
    expect(wrong.status).toBe(401);

    const msg = await post(
      r.baseUrl,
      "/converse/sessions/whatever/messages",
      { tenantId: "t1", text: "hi", contextPacket: PACKET },
    );
    expect(msg.status).toBe(401);

    const tr = await get(r.baseUrl, "/converse/sessions/whatever/transcript");
    expect(tr.status).toBe(401);

    const ok = await post(
      r.baseUrl,
      "/converse/sessions",
      { tenantId: "t1", drawingId: "d1" },
      { "x-harness-secret": SECRET },
    );
    expect(ok.status).toBe(200);
    expect(ok.json.sessionId).toBeTruthy();
  });
});

// --------------------------------------------------------------------------- //
// Wiring guard
// --------------------------------------------------------------------------- //

describe("/converse routes — unwired ports", () => {
  it("501 when the converse ports are not configured", async () => {
    const r = rig({ omitConverse: true });
    const { status } = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1", drawingId: "d1" });
    expect(status).toBe(501);
  });
});

// --------------------------------------------------------------------------- //
// Session create
// --------------------------------------------------------------------------- //

describe("POST /converse/sessions", () => {
  it("is idempotent per (tenant, drawing) and returns {sessionId, status, createdAt}", async () => {
    const r = rig();
    const first = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1", drawingId: "d1" });
    expect(first.status).toBe(200);
    expect(first.json.status).toBe("idle");
    expect(first.json.createdAt).toBeTruthy();

    const again = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1", drawingId: "d1" });
    expect(again.json.sessionId).toBe(first.json.sessionId);

    const otherDrawing = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1", drawingId: "d2" });
    expect(otherDrawing.json.sessionId).not.toBe(first.json.sessionId);

    const otherTenant = await post(r.baseUrl, "/converse/sessions", { tenantId: "t2", drawingId: "d1" });
    expect(otherTenant.json.sessionId).not.toBe(first.json.sessionId);
  });

  it("400 without a drawingId", async () => {
    const r = rig();
    const { status } = await post(r.baseUrl, "/converse/sessions", { tenantId: "t1" });
    expect(status).toBe(400);
  });
});

// --------------------------------------------------------------------------- //
// Messages: 202 / 409 / 401 grant_required / 404 tenant mismatch
// --------------------------------------------------------------------------- //

describe("POST /converse/sessions/{id}/messages", () => {
  it("202 {turnId}; the turn persists section-3 events readable via /transcript", async () => {
    const r = rig();
    const sessionId = await createSession(r);
    const turnId = await sendAndFinish(r, sessionId, "t1", "hello");

    const { status, json } = await get(
      r.baseUrl,
      `/converse/sessions/${sessionId}/transcript?tenantId=t1`,
    );
    expect(status).toBe(200);
    const events = json.events as ConverseEvent[];
    const types = events.map((e) => e.type);
    expect(types[0]).toBe("turn_started");
    expect(types).toContain("text_delta");
    expect(types[types.length - 1]).toBe("turn_complete");
    for (const ev of events) {
      expect(ev.v).toBe(1);
      expect(ev.session_id).toBe(sessionId);
      expect(ev.turn_id).toBe(turnId);
    }
    // seq strictly ascending
    const seqs = events.map((e) => e.seq);
    expect([...seqs].sort((a, b) => a - b)).toEqual(seqs);

    // transcript?limit=N returns the most recent N, still ascending
    const limited = await get(
      r.baseUrl,
      `/converse/sessions/${sessionId}/transcript?tenantId=t1&limit=2`,
    );
    const lastTwo = (limited.json.events as ConverseEvent[]).map((e) => e.seq);
    expect(lastTwo).toEqual(seqs.slice(-2));
  });

  it("409 {error:'turn_in_progress', turnId} while a turn is active", async () => {
    const r = rig();
    const sessionId = await createSession(r);
    let release!: () => void;
    r.runner.slowGate = new Promise<void>((res) => {
      release = res;
    });

    const first = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
      tenantId: "t1",
      text: "SLOW please",
      contextPacket: PACKET,
    });
    expect(first.status).toBe(202);

    const second = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
      tenantId: "t1",
      text: "hello again",
      contextPacket: PACKET,
    });
    expect(second.status).toBe(409);
    expect(second.json).toMatchObject({ error: "turn_in_progress", turnId: first.json.turnId });

    release();
  });

  it("401 {error:'grant_required'} when the live runner factory has no grant", async () => {
    const r = rig({
      converseRunnerFor: async (tenantId: string) => {
        throw new GrantRequiredError(tenantId);
      },
    });
    const sessionId = await createSession(r);
    const { status, json } = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
      tenantId: "t1",
      text: "hello",
      contextPacket: PACKET,
    });
    expect(status).toBe(401);
    expect(json).toEqual({ error: "grant_required" });
  });

  it("tenant mismatch answers 404 session_not_found on messages/transcript/delete", async () => {
    const r = rig();
    const sessionId = await createSession(r, "t1", "d1");

    const msg = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
      tenantId: "t2",
      text: "hello",
      contextPacket: PACKET,
    });
    expect(msg.status).toBe(404);
    expect(msg.json).toEqual({ error: "session_not_found" });

    const tr = await get(r.baseUrl, `/converse/sessions/${sessionId}/transcript?tenantId=t2`);
    expect(tr.status).toBe(404);

    const delRes = await fetch(`${r.baseUrl}/converse/sessions/${sessionId}`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenantId: "t2" }),
    });
    expect(delRes.status).toBe(404);
  });

  it("DELETE archives for the owning tenant; the archived session then answers 404", async () => {
    const r = rig();
    const sessionId = await createSession(r, "t1", "d1");
    const delRes = await fetch(`${r.baseUrl}/converse/sessions/${sessionId}`, {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenantId: "t1" }),
    });
    expect(delRes.status).toBe(200);
    expect(await delRes.json()).toEqual({ archived: true });

    const after = await post(r.baseUrl, `/converse/sessions/${sessionId}/messages`, {
      tenantId: "t1",
      text: "hello",
      contextPacket: PACKET,
    });
    expect(after.status).toBe(404);
  });

  it("DELETE accepts tenantId as a query param, body-free (how the app relay sends it)", async () => {
    const r = rig();
    const sessionId = await createSession(r, "t1", "d1");

    // Wrong tenant in the query still answers 404 (no existence oracle).
    const wrong = await fetch(`${r.baseUrl}/converse/sessions/${sessionId}?tenantId=t2`, {
      method: "DELETE",
    });
    expect(wrong.status).toBe(404);

    const delRes = await fetch(`${r.baseUrl}/converse/sessions/${sessionId}?tenantId=t1`, {
      method: "DELETE",
    });
    expect(delRes.status).toBe(200);
    expect(await delRes.json()).toEqual({ archived: true });
    expect((await r.store.getSession(sessionId))?.status).toBe("archived");
  });
});

// --------------------------------------------------------------------------- //
// Stream: replay afterSeq + live tail
// --------------------------------------------------------------------------- //

describe("GET /converse/sessions/{id}/stream", () => {
  it("replays ONLY events with seq > afterSeq from the store", async () => {
    const r = rig();
    const sessionId = await createSession(r);
    await sendAndFinish(r, sessionId, "t1", "hello");
    const all = await r.store.eventsAfter(sessionId, 0);
    expect(all.length).toBeGreaterThanOrEqual(3);
    const afterSeq = all[1]!.seq; // skip the first two

    const events = await readSse(
      r.baseUrl,
      `/converse/sessions/${sessionId}/stream?tenantId=t1&afterSeq=${afterSeq}`,
      (evs) => evs.some((e) => e.type === "turn_complete"),
    );
    expect(events.length).toBe(all.length - 2);
    expect(events.every((e) => e.seq > afterSeq)).toBe(true);
    expect(events[events.length - 1]!.type).toBe("turn_complete");
  });

  it("404 session_not_found for a foreign tenant's stream", async () => {
    const r = rig();
    const sessionId = await createSession(r);
    const { status, json } = await get(
      r.baseUrl,
      `/converse/sessions/${sessionId}/stream?tenantId=t2`,
    );
    expect(status).toBe(404);
    expect(json).toEqual({ error: "session_not_found" });
  });

  it("live-tails a turn started AFTER the stream was opened", async () => {
    const r = rig();
    const sessionId = await createSession(r);

    const tail = readSse(
      r.baseUrl,
      `/converse/sessions/${sessionId}/stream?tenantId=t1&afterSeq=0`,
      (evs) => evs.some((e) => e.type === "turn_complete"),
    );
    // Give the stream a beat to attach its live sink, then start the turn.
    await new Promise((res) => setTimeout(res, 50));
    await sendAndFinish(r, sessionId, "t1", "hello live");

    const events = await tail;
    const types = events.map((e) => e.type);
    expect(types[0]).toBe("turn_started");
    expect(types).toContain("text_delta");
    expect(types[types.length - 1]).toBe("turn_complete");
    const seqs = events.map((e) => e.seq);
    expect([...seqs].sort((a, b) => a - b)).toEqual(seqs);
  });
});
