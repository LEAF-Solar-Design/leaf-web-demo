/**
 * Harness HTTP server - a thin shell over AuthorLoop. Ports are INJECTED, so the
 * hermetic tests wire fakes and the operator-gated `startReal()` wires the real
 * impls. See harness/contract/HARNESS-CONTRACT.md for the full API.
 *
 * Routes:
 *   GET  /health          -> { ok: true }
 *   POST /author          -> author route (build | one-off). Body {description, mode?}.
 *                            build:   200 { tool, code, preview }         (CONTRACT section 4)
 *                            one-off: 200 { tool, code, preview, run }
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
import { GrantRequiredError } from "./ports/impl/oauthGrantProvider.js";
import { classifyRoute } from "./routing.js";
import { DEFAULT_TENANT } from "./ports/index.js";
import type { HarnessPorts } from "./ports/index.js";

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
 */
export function createHarness(ports: HarnessPorts, opts?: { auth?: HarnessAuthConfig }): Harness {
  const loop = new AuthorLoop(ports);
  const explicitAuth = opts?.auth ?? null;

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

  const tenantsDir = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
  const grantStore = new FileTenantGrantStore(); // per-tenant grant + admin (one store)

  const ports: HarnessPorts = {
    agentRunner: new AgentSdkRunner(),
    broker: new BrokerApsClientHttp(),
    oauth: new OAuthGrantProviderImpl({ store: grantStore }),
    grantAdmin: grantStore,
    tenantRepo: new TenantRepoProviderImpl({
      locator: { async repoRef(t) { return `${tenantsDir}/${t}`; } },
      inPlace: true,
    }),
  };
  return createHarness(ports).listen(port);
}
