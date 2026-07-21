/**
 * Harness HTTP server - a thin shell over AuthorLoop. Ports are INJECTED, so the
 * hermetic tests wire fakes and the operator-gated `startReal()` wires the real
 * impls. See harness/contract/HARNESS-CONTRACT.md for the full API.
 *
 * Routes:
 *   GET  /health          -> { ok: true }
 *   POST /author          -> author route (build | one-off). Body {description, mode?}.
 *                            build:   200 { tool, code, preview, telemetry? } (CONTRACT section 4)
 *                            one-off: 200 { tool, code, preview, run, telemetry? }
 *                            `telemetry` (A1) is ADDITIVE + OPTIONAL — a provenance chip source:
 *                            { turns?, input_tokens?, output_tokens?, total_cost_usd?, models?[] }.
 *                            It is present ONLY when the runner metered the build (the real Agent
 *                            SDK runner); absent-safe, so a non-metering runner keeps the frozen
 *                            {tool, code, preview} shape. Forwarded verbatim from AuthorLoop.
 *   POST /run-registered  -> run route (design-time-ONLY invariant). Body
 *                            {tool, params?, dwg?, aps_live?}. 200 section-3 envelope.
 *                            NEVER constructs the Agent SDK / touches AgentRunner.
 *
 * Tenant id comes from the X-Tenant-Id header stub (default "demo-tenant"),
 * matching the backbone. Concern 1 (Auth0 platform identity) is resolved upstream
 * and is NOT this harness's job; Concern 2 (Claude grant) is the OAuthGrantProvider.
 *
 * CALLER AUTH (F5): the HTTP surface is NOT public. When the gate is enabled
 * (LEAF_HARNESS_AUTH truthy in the live serve path), EVERY route except GET /health
 * requires a shared secret on the `X-Harness-Secret` header, constant-time compared
 * against LEAF_HARNESS_SECRET; a wrong/absent secret is 401. The gate is DEFAULT-OFF
 * so the hermetic/local path (and `npm test`) is unchanged. On a reachable harness the
 * ONLY legitimate caller is the app (server/routers/author.py, server/routers/tenant.py),
 * which forwards the secret from the same env; the per-request tenant identity therefore
 * comes from the AUTHENTICATED app, never from an anonymous request body. The secret is
 * env-only: never logged, never echoed, never mingled with any grant token.
 */

import { createHash, timingSafeEqual } from "node:crypto";
import { createServer as createHttpServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { AuthorLoop, AuthorLoopError } from "./agent/authorLoop.js";
import {
  BadMessageError,
  ConfirmationInvalidError,
  ConverseLoop,
  SessionNotFoundError,
  TurnInProgressError,
} from "./agent/converseLoop.js";
import { GrantRequiredError } from "./ports/impl/oauthGrantProvider.js";
import { classifyRoute } from "./routing.js";
import { DEFAULT_TENANT } from "./ports/index.js";
import type { ConverseEvent, ConverseRunner, HarnessPorts } from "./ports/index.js";

export { DEFAULT_TENANT };

function tenantOf(req: IncomingMessage): string {
  const h = req.headers["x-tenant-id"];
  const v = Array.isArray(h) ? h[0] : h;
  return v && v.trim() ? v.trim() : DEFAULT_TENANT;
}

/** Resolve the request tenant: body `tenant_id` wins, else the X-Tenant-Id header,
 *  else DEFAULT_TENANT. Lets the app forward the RESOLVED tenant per request.
 *
 *  SECURITY NOTE (F5): the body `tenant_id` is TRUSTED only because a reachable harness
 *  is gated by the shared secret (see harnessAuthDenial) — the sole caller that can reach
 *  this code is the AUTHENTICATED app, which forwards the tenant it already resolved from
 *  the platform JWT. This is NOT anonymous self-assertion of identity: without the secret
 *  the request never reaches tenant resolution. Do not expose this harness unauthenticated. */
function tenantForRequest(req: IncomingMessage, body: Record<string, unknown>): string {
  const t = body.tenant_id;
  if (typeof t === "string" && t.trim()) return t.trim();
  return tenantOf(req);
}

// --------------------------------------------------------------------------- //
// F5 caller auth — shared-secret gate on the harness HTTP surface.
// --------------------------------------------------------------------------- //

/** Shared-secret gate config. DEFAULT-OFF (enabled=false) keeps the hermetic/local path
 *  and `npm test` unchanged; the live serve path turns it on via env (LEAF_HARNESS_AUTH). */
export interface HarnessAuthConfig {
  /** When false, every route is served as before (no secret required). */
  enabled: boolean;
  /** Expected shared secret (from LEAF_HARNESS_SECRET). Never logged, never echoed. */
  secret: string;
}

const AUTH_ENV_FLAG = "LEAF_HARNESS_AUTH";
const AUTH_ENV_SECRET = "LEAF_HARNESS_SECRET";

/** Env truthiness matching the app's flag pattern: "1"/"true"/"yes"/"on" (case-insensitive). */
function envFlagOn(v: string | undefined): boolean {
  const s = (v ?? "").trim().toLowerCase();
  return s === "1" || s === "true" || s === "yes" || s === "on";
}

/**
 * Resolve the auth gate from the environment (the live serve path uses this per request,
 * so setting LEAF_HARNESS_AUTH + LEAF_HARNESS_SECRET is all the wiring serve.ts needs).
 * LIVE FAIL-CLOSED: when the gate is ON but LEAF_HARNESS_SECRET is unset/empty, `secret`
 * is "" and harnessAuthDenial rejects EVERYTHING (an anonymous harness is never open).
 */
export function resolveHarnessAuth(env: NodeJS.ProcessEnv = process.env): HarnessAuthConfig {
  return { enabled: envFlagOn(env[AUTH_ENV_FLAG]), secret: (env[AUTH_ENV_SECRET] ?? "").trim() };
}

/**
 * Constant-time secret compare. Both sides are hashed to a fixed-length SHA-256 digest so
 * the comparison is length-independent (timingSafeEqual requires equal-length buffers) and
 * leaks neither the secret's length nor its bytes via timing.
 */
function secretsEqual(provided: string, expected: string): boolean {
  const a = createHash("sha256").update(provided, "utf8").digest();
  const b = createHash("sha256").update(expected, "utf8").digest();
  return timingSafeEqual(a, b);
}

/**
 * Auth decision for one request. Returns a 401 error body when the request MUST be
 * rejected, or null when it is authorized (or the gate is off). GET /health is exempt at
 * the call site. The secret is NEVER included in the returned body.
 */
function harnessAuthDenial(
  req: IncomingMessage,
  auth: HarnessAuthConfig,
): { error: { message: string; code: string } } | null {
  if (!auth.enabled) return null; // default-off: hermetic/local path unchanged
  // Fail-closed: an enabled gate with no configured secret authenticates NOTHING. This is
  // the guard that stops an empty provided secret from matching an empty expected secret.
  if (!auth.secret) {
    return { error: { message: "harness auth required", code: "harness_auth_required" } };
  }
  const h = req.headers["x-harness-secret"];
  const provided = Array.isArray(h) ? h[0] : h;
  if (typeof provided !== "string" || provided.length === 0 || !secretsEqual(provided, auth.secret)) {
    return { error: { message: "harness auth required", code: "harness_auth_required" } };
  }
  return null;
}

function readJsonBody(req: IncomingMessage): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf8").trim();
      if (!raw) return resolve({});
      try {
        resolve(JSON.parse(raw) as Record<string, unknown>);
      } catch (e) {
        reject(new AuthorLoopError(`invalid JSON body: ${(e as Error).message}`, 400));
      }
    });
    req.on("error", reject);
  });
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(payload);
}

export interface Harness {
  handler: (req: IncomingMessage, res: ServerResponse) => Promise<void>;
  listen(port?: number): Server;
  loop: AuthorLoop;
}

/**
 * Create the harness. `opts.auth` explicitly pins the F5 caller-auth gate (used by tests
 * to stay hermetic); when omitted the gate resolves per-request from the environment via
 * resolveHarnessAuth() — so the live serve path needs no code change, only the env vars.
 *
 * `opts.converseRunnerFor` is the LIVE converse-runner source: a factory building a
 * FRESH runner for one tenant's grant (called per turn, so no runner instance — and no
 * telemetry — is ever shared across sessions). It may throw GrantRequiredError, which
 * the messages route maps to the wire 401 {error:"grant_required"}. When absent,
 * `ports.converseRunner` (the hermetic shared fake) is used instead.
 */
export function createHarness(
  ports: HarnessPorts,
  opts?: {
    auth?: HarnessAuthConfig;
    converseRunnerFor?: (tenantId: string) => Promise<ConverseRunner>;
  },
): Harness {
  const loop = new AuthorLoop(ports);
  const explicitAuth = opts?.auth ?? null;

  // Converse surface (wire contract section 1): mounted only when the section-18
  // ports are wired; otherwise those routes answer 501 (grant-admin precedent).
  const converse =
    ports.appRun && ports.gate && ports.sessionStore
      ? { appRun: ports.appRun, gate: ports.gate, store: ports.sessionStore }
      : null;

  // Live SSE fan-out hub: every ConverseLoop event is persisted FIRST (store seq
  // is truth), then broadcast here so open /stream tails see it without polling.
  const liveSinks = new Map<string, Set<(ev: ConverseEvent) => void>>();
  const broadcast = (ev: ConverseEvent): void => {
    const sinks = liveSinks.get(ev.session_id);
    if (!sinks) return;
    for (const sink of sinks) {
      try {
        sink(ev);
      } catch {
        // a broken subscriber never corrupts the turn; the transcript has the event
      }
    }
  };

  /** Wire envelope (section 3) from a stored row or a live event. */
  const toWire = (ev: {
    session_id: string;
    turn_id: string;
    seq: number;
    type: string;
    data: Record<string, unknown>;
  }): ConverseEvent => ({
    v: 1,
    session_id: ev.session_id,
    turn_id: ev.turn_id,
    seq: ev.seq,
    type: ev.type as ConverseEvent["type"],
    data: ev.data,
  });

  /** Session lookup with the tenant-mismatch rule: mismatch answers EXACTLY like
   *  absence (404 session_not_found; no cross-tenant existence oracle). */
  const sessionForTenant = async (sessionId: string, tenantId: string) => {
    if (!converse) return null;
    const s = await converse.store.getSession(sessionId);
    if (!s || s.tenant_id !== tenantId || s.status === "archived") return null;
    return s;
  };

  /** Tenant for a converse request: body tenantId (camelCase per wire contract
   *  section 1) wins, else the X-Tenant-Id header, else DEFAULT_TENANT. Trusted
   *  for the same reason as tenantForRequest (the F5 shared-secret gate). */
  const converseTenant = (req: IncomingMessage, body: Record<string, unknown>): string => {
    const t = body.tenantId;
    if (typeof t === "string" && t.trim()) return t.trim();
    return tenantOf(req);
  };

  const handleConverse = async (
    req: IncomingMessage,
    res: ServerResponse,
    method: string,
    path: string,
  ): Promise<void> => {
    if (!converse || (!ports.converseRunner && !opts?.converseRunnerFor)) {
      return send(res, 501, { error: { message: "converse ports not configured" } });
    }
    const url = new URL(req.url ?? "/", "http://converse.local");

    // POST /converse/sessions — idempotent create-or-get per (tenant, drawing).
    if (method === "POST" && path === "/converse/sessions") {
      const body = await readJsonBody(req);
      const tenantId = converseTenant(req, body);
      const drawingId = typeof body.drawingId === "string" ? body.drawingId.trim() : "";
      if (!drawingId) return send(res, 400, { error: { message: "drawingId is required" } });
      const session = await converse.store.createOrGetSession(tenantId, drawingId);
      return send(res, 200, {
        sessionId: session.session_id,
        status: session.status,
        createdAt: session.created_at,
      });
    }

    const m = /^\/converse\/sessions\/([^/]+)(?:\/(messages|stream|transcript))?$/.exec(path);
    if (!m) return send(res, 404, { error: { message: `no route for ${method} ${path}` } });
    const sessionId = decodeURIComponent(m[1]!);
    const sub = m[2];

    // POST /converse/sessions/{id}/messages — start a turn (202 async).
    if (method === "POST" && sub === "messages") {
      const body = await readJsonBody(req);
      const tenantId = converseTenant(req, body);

      // Resolve the runner FIRST: the live factory needs the tenant's grant, and a
      // missing grant must 401 before any turn state is touched.
      let runner: ConverseRunner;
      try {
        runner = ports.converseRunner ?? (await opts!.converseRunnerFor!(tenantId));
      } catch (e) {
        if (e instanceof GrantRequiredError) {
          return send(res, 401, { error: "grant_required" });
        }
        throw e;
      }

      const rawConfirm = body.confirm as Record<string, unknown> | undefined;
      const confirm =
        rawConfirm &&
        typeof rawConfirm.confirmationId === "string" &&
        typeof rawConfirm.approved === "boolean"
          ? { confirmationId: rawConfirm.confirmationId, approved: rawConfirm.approved }
          : undefined;

      const converseLoop = new ConverseLoop({ runner, ...converse });
      try {
        const { turnId, done } = await converseLoop.handleMessage({
          sessionId,
          tenantId,
          ...(typeof body.text === "string" ? { text: body.text } : {}),
          ...(confirm ? { confirm } : {}),
          contextPacket:
            body.contextPacket && typeof body.contextPacket === "object"
              ? (body.contextPacket as Record<string, unknown>)
              : {},
          classifierHint:
            body.classifierHint && typeof body.classifierHint === "object"
              ? (body.classifierHint as Record<string, unknown>)
              : null,
          onEvent: broadcast,
        });
        // 202: the turn runs on; its outcome is persisted (and streamed) — a turn
        // failure after this point is a transcript fact, not an HTTP error.
        done.catch(() => undefined);
        return send(res, 202, { turnId });
      } catch (e) {
        if (e instanceof TurnInProgressError) {
          return send(res, 409, { error: "turn_in_progress", turnId: e.turnId });
        }
        if (e instanceof SessionNotFoundError) {
          return send(res, 404, { error: "session_not_found" });
        }
        if (e instanceof BadMessageError) {
          return send(res, 400, { error: { message: e.message } });
        }
        if (e instanceof ConfirmationInvalidError) {
          return send(res, e.reason === "expired" ? 410 : 400, {
            error: { message: e.message, reason: e.reason },
          });
        }
        throw e;
      }
    }

    // GET /converse/sessions/{id}/stream?afterSeq=N — replay from the store, then
    // live-tail loop events. Tenant rides the query (app relay) or the header.
    if (method === "GET" && sub === "stream") {
      const tenantId = (url.searchParams.get("tenantId") ?? "").trim() || tenantOf(req);
      const session = await sessionForTenant(sessionId, tenantId);
      if (!session) return send(res, 404, { error: "session_not_found" });
      const afterSeq = Math.max(0, Number(url.searchParams.get("afterSeq") ?? "0") || 0);

      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive",
      });
      let lastSeq = afterSeq;
      const writeEvent = (ev: ConverseEvent): void => {
        if (ev.seq <= lastSeq) return; // monotonic guard dedups replay/live overlap
        lastSeq = ev.seq;
        try {
          res.write(`event: ${ev.type}\ndata: ${JSON.stringify(ev)}\n\n`);
        } catch {
          // a torn client connection must never take down the handler
        }
      };

      // Register the live sink BEFORE the replay read (buffering until replay is
      // written) so no event can fall between replay and live registration; the
      // seq guard drops any overlap duplicates.
      let live = false;
      const pending: ConverseEvent[] = [];
      const sink = (ev: ConverseEvent): void => {
        if (live) writeEvent(ev);
        else pending.push(ev);
      };
      let sinks = liveSinks.get(sessionId);
      if (!sinks) {
        sinks = new Set();
        liveSinks.set(sessionId, sinks);
      }
      sinks.add(sink);
      const ping = setInterval(() => {
        try {
          res.write(": ping\n\n");
        } catch {
          // ignore; close cleanup below tears the tail down
        }
      }, 15_000);
      ping.unref?.();
      req.on("close", () => {
        clearInterval(ping);
        sinks.delete(sink);
        if (sinks.size === 0) liveSinks.delete(sessionId);
      });

      const replay = await converse.store.eventsAfter(sessionId, afterSeq);
      for (const ev of replay) writeEvent(toWire(ev));
      for (const ev of pending) writeEvent(ev);
      pending.length = 0;
      live = true;
      return; // response intentionally left open (SSE)
    }

    // GET /converse/sessions/{id}/transcript?limit=N — most recent N, ascending seq.
    if (method === "GET" && sub === "transcript") {
      const tenantId = (url.searchParams.get("tenantId") ?? "").trim() || tenantOf(req);
      const session = await sessionForTenant(sessionId, tenantId);
      if (!session) return send(res, 404, { error: "session_not_found" });
      const limitRaw = Number(url.searchParams.get("limit") ?? "");
      const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.trunc(limitRaw) : undefined;
      const events = await converse.store.eventsAfter(sessionId, 0, limit);
      return send(res, 200, { events: events.map(toWire) });
    }

    // DELETE /converse/sessions/{id} — archive (the store keeps the transcript).
    // The app relay sends tenantId as a QUERY param (like stream/transcript);
    // JSON body / header stay accepted for direct callers.
    if (method === "DELETE" && sub === undefined) {
      const body = await readJsonBody(req);
      const tenantId =
        (url.searchParams.get("tenantId") ?? "").trim() || converseTenant(req, body);
      const session = await sessionForTenant(sessionId, tenantId);
      if (!session) return send(res, 404, { error: "session_not_found" });
      await converse.store.updateSession(sessionId, { status: "archived" });
      return send(res, 200, { archived: true });
    }

    return send(res, 405, { error: { message: `method ${method} not allowed on ${path}` } });
  };

  const handler = async (req: IncomingMessage, res: ServerResponse): Promise<void> => {
    const path = (req.url ?? "").split("?")[0];
    const method = req.method ?? "GET";
    try {
      if (method === "GET" && path === "/health") {
        return send(res, 200, { ok: true, service: "leaf-tenant-author-harness" });
      }

      // F5 caller-auth gate: every non-health route requires the shared secret when the
      // gate is enabled. Rejected BEFORE any body read, tenant resolution, or store touch,
      // so an unauthed caller can neither author, run, nor read/overwrite/delete a grant.
      const auth = explicitAuth ?? resolveHarnessAuth();
      const denial = harnessAuthDenial(req, auth);
      if (denial) return send(res, 401, denial);

      // Conversational spine surface (wire contract section 1) — behind the SAME
      // X-Harness-Secret gate as every other route.
      if (path === "/converse/sessions" || path.startsWith("/converse/sessions/")) {
        return handleConverse(req, res, method, path ?? "");
      }

      // Per-tenant grant admin (wave 4 + §17): PUT/GET/DELETE /grants/{tenantId}. Backs
      // the app's /api/tenant/claude-grant proxy. Returns ONLY {linked, linked_at, kind}
      // — never the token. PUT body may carry an optional `kind` (else auto-detected).
      // 501 when no grantAdmin store is wired (the hermetic author tests).
      if (path.startsWith("/grants/")) {
        const tenantId = decodeURIComponent(path.slice("/grants/".length));
        if (!tenantId) return send(res, 400, { error: { message: "tenant id required" } });
        if (!ports.grantAdmin) {
          return send(res, 501, { error: { message: "grant admin store not configured" } });
        }
        try {
          if (method === "PUT") {
            const gbody = await readJsonBody(req);
            const token = typeof gbody.token === "string" ? gbody.token : "";
            if (!token.trim()) return send(res, 400, { error: { message: "token is required" } });
            // Optional explicit kind; when absent/invalid the store AUTO-DETECTS from the
            // token prefix (§17). Only the two valid literals are forwarded.
            const rawKind = typeof gbody.kind === "string" ? gbody.kind.trim() : "";
            const kind = rawKind === "oauth" || rawKind === "api_key" ? rawKind : undefined;
            const st = await ports.grantAdmin.put(tenantId, token, kind);
            return send(res, 200, st); // {linked, linked_at, kind} — token never echoed
          }
          if (method === "GET") {
            return send(res, 200, await ports.grantAdmin.status(tenantId));
          }
          if (method === "DELETE") {
            await ports.grantAdmin.remove(tenantId);
            return send(res, 200, { linked: false, linked_at: null });
          }
        } catch (e) {
          // e.g. a malformed/traversal tenant id — a client error, never a token leak.
          return send(res, 400, { error: { message: (e as Error).message } });
        }
        return send(res, 405, { error: { message: `method ${method} not allowed on ${path}` } });
      }

      if (method === "POST" && path === "/author") {
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        const description = typeof body.description === "string" ? body.description : "";
        if (!description.trim()) {
          return send(res, 400, { error: { message: "description is required" } });
        }
        const route = classifyRoute({ path, body });
        if (route === "one-off") {
          const out = await loop.oneOff(tenant, description);
          return send(res, 200, out);
        }
        // default author route = build (author + register + one commit)
        const out = await loop.build(tenant, description);
        return send(res, 200, out);
      }

      if (method === "POST" && path === "/run-registered") {
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        const toolName = typeof body.tool === "string" ? body.tool : "";
        if (!toolName) {
          return send(res, 400, { error: { message: "tool (registered tool name) is required" } });
        }
        const params = (body.params as Record<string, unknown>) ?? {};
        const dwg = typeof body.dwg === "string" ? body.dwg : "rooftop_demo";
        const apsLive = body.aps_live === true;
        const envelope = await loop.run(tenant, toolName, params, dwg, apsLive);
        return send(res, 200, envelope);
      }

      return send(res, 404, { error: { message: `no route for ${method} ${path}` } });
    } catch (err) {
      // Diagnostic: full stack to stderr (never contains the grant; see serve.ts note).
      console.error("[harness] request error:", (err as Error).stack ?? String(err));
      // Missing per-tenant grant -> a clean 401 with a machine-detectable marker the app
      // proxy maps to GRANT_REQUIRED (frontend prompts "sign in with Claude"). No token.
      if (err instanceof GrantRequiredError) {
        return send(res, 401, {
          grant_required: true,
          error: { message: err.message, code: "grant_required" },
        });
      }
      if (err instanceof AuthorLoopError) {
        return send(res, err.status, {
          error: { message: err.message, diagnostics: err.diagnostics ?? [] },
        });
      }
      return send(res, 500, { error: { message: (err as Error).message } });
    }
  };

  return {
    handler,
    loop,
    listen(port = 0): Server {
      const server = createHttpServer((req, res) => {
        void handler(req, res);
      });
      server.listen(port);
      return server;
    },
  };
}

/**
 * OPERATOR-GATED real wiring. Not auto-run and not exercised by the hermetic gate.
 * It only COMPILES here to prove the real ports slot in unchanged. To run live the
 * operator must: register the app for "sign in with Claude", install
 * @anthropic-ai/claude-agent-sdk, run the broker (BROKER_URL), and provide the
 * per-tenant grant store + repo locator. See HARNESS-CONTRACT.md.
 */
export async function startReal(port = 8130): Promise<Server> {
  const { AgentSdkRunner } = await import("./ports/impl/agentSdkRunner.js");
  const { BrokerApsClientHttp } = await import("./ports/impl/brokerApsClient.js");
  const { FileTenantGrantStore, OAuthGrantProviderImpl } = await import("./ports/impl/oauthGrantProvider.js");
  const { TenantRepoProviderImpl } = await import("./ports/impl/tenantRepoProvider.js");
  const { HttpAppRunClient } = await import("./ports/impl/appRunClient.js");
  const { HttpGateClient } = await import("./ports/impl/gateClient.js");
  const { FileSessionStore } = await import("./ports/impl/sessionStore.js");
  const { ConverseSdkRunner } = await import("./ports/impl/converseSdkRunner.js");

  const tenantsDir = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
  const grantStore = new FileTenantGrantStore(); // per-tenant grant + admin (one store)
  const oauth = new OAuthGrantProviderImpl({ store: grantStore });

  // Converse back-edge (wire contract section 0): app base URL + dispatch secret.
  // With LEAF_APP_DISPATCH_SECRET unset the app rejects the back-edge (401) and the
  // gate client fails CLOSED, so an unconfigured deployment cannot dispatch anything.
  const appUrl = process.env.LEAF_APP_URL ?? "http://127.0.0.1:8130";
  const dispatchSecret = (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim();

  const ports: HarnessPorts = {
    agentRunner: new AgentSdkRunner(),
    broker: new BrokerApsClientHttp(),
    oauth,
    grantAdmin: grantStore,
    tenantRepo: new TenantRepoProviderImpl({
      locator: { async repoRef(t) { return `${tenantsDir}/${t}`; } },
      inPlace: true,
    }),
    appRun: new HttpAppRunClient({ baseUrl: appUrl, dispatchSecret }),
    gate: new HttpGateClient({ appBaseUrl: appUrl, dispatchSecret }),
    sessionStore: new FileSessionStore(),
  };
  return createHarness(ports, {
    // A FRESH live runner per turn, for THIS tenant's grant: no shared instance,
    // no cross-tenant telemetry bleed. GrantRequiredError -> 401 grant_required.
    converseRunnerFor: async (tenantId) =>
      new ConverseSdkRunner({ grant: await oauth.getGrant(tenantId) }),
  }).listen(port);
}
