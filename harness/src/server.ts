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
 *   POST /turn             -> converse-turn route (sessions wire, leaf-backend-gaps.md
 *                            §2.1; ports/converse.ts, FROZEN). Body = ConverseTurnInput.
 *                            200 application/x-ndjson, one HarnessTurnEvent per line,
 *                            always terminated by turn_complete or error. A grant error
 *                            resolved on/before the first event -> non-stream 401
 *                            {grant_required:true,...} (never a half-open stream). No
 *                            converseRunner wired -> 501. Bad body -> 400.
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
import { redactTokens } from "./redact.js";
import { GrantPoolUnavailableError, GrantRequiredError } from "./ports/impl/oauthGrantProvider.js";
import { classifyRoute } from "./routing.js";
import { DEFAULT_TENANT } from "./ports/index.js";
import type { ConverseRunner, ConverseTurnInput, HarnessPorts, HarnessTurnEvent,
  InstantDrawingContext, InstantSessionAssignment } from "./ports/index.js";
import { ALLOWED_MODELS, isAllowedModel } from "./ports/modelAllowlist.js";
import { parseWireGrant } from "./ports/wireGrant.js";
import { authoredExecutionEnabled } from "./runtimeSafety.js";

export { DEFAULT_TENANT };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

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

function requiredText(body: Record<string, unknown>, field: string): string {
  const value = body[field];
  if (typeof value !== "string" || !value.trim()) {
    throw new AuthorLoopError(`${field} is required`, 400);
  }
  return value.trim();
}

function send(res: ServerResponse, status: number, body: unknown): void {
  const payload = JSON.stringify(body);
  res.writeHead(status, { "content-type": "application/json" });
  res.end(payload);
}

// --------------------------------------------------------------------------- //
// POST /turn - converse-turn route (sessions wire, leaf-backend-gaps.md §2.1).
// --------------------------------------------------------------------------- //

type ConverseTurnValidation = {
  ok: true;
  input: ConverseTurnInput;
  instantAssignment?: InstantSessionAssignment;
  instantDrawingContext?: InstantDrawingContext;
} | { ok: false; message: string };

/**
 * Minimal validation of ConverseTurnInput: the four required id fields, and at
 * least one of `text` / `confirm` to drive the turn. Deliberately does not
 * validate deeper into `confirm.proposal` — that is the runner's job. `messages`
 * defaults to [] when absent/malformed (the turn engine always sends it, but a
 * missing array here should not be a reason to reject the whole request).
 */
function validateConverseTurnInput(body: Record<string, unknown>): ConverseTurnValidation {
  const tenant_id = typeof body.tenant_id === "string" ? body.tenant_id.trim() : "";
  const session_id = typeof body.session_id === "string" ? body.session_id.trim() : "";
  const turn_id = typeof body.turn_id === "string" ? body.turn_id.trim() : "";
  const drawing_id = typeof body.drawing_id === "string" ? body.drawing_id.trim() : "";
  if (!tenant_id) return { ok: false, message: "tenant_id is required" };
  if (!session_id) return { ok: false, message: "session_id is required" };
  if (!turn_id) return { ok: false, message: "turn_id is required" };
  if (!drawing_id) return { ok: false, message: "drawing_id is required" };

  const hasText = typeof body.text === "string" && body.text.length > 0;
  const rawConfirm = body.confirm;
  const hasConfirm = typeof rawConfirm === "object" && rawConfirm !== null;
  if (!hasText && !hasConfirm) {
    return { ok: false, message: "one of text or confirm is required" };
  }

  const messages = Array.isArray(body.messages)
    ? (body.messages as ConverseTurnInput["messages"])
    : [];

  const input: ConverseTurnInput = { tenant_id, session_id, turn_id, drawing_id, messages };
  if (hasText) input.text = body.text as string;
  if (hasConfirm) input.confirm = rawConfirm as ConverseTurnInput["confirm"];

  // Per-session "mount your LLM" fields — OPTIONAL and additive. Validated here
  // as defense in depth (the app already validates); an unknown model or a
  // malformed credential grant is a 400, never a silent pass-through to sdk.query.
  if (body.model !== undefined) {
    if (!isAllowedModel(body.model)) {
      return { ok: false, message: `model must be one of: ${ALLOWED_MODELS.join(", ")}` };
    }
    input.model = body.model;
  }
  if (body.credential_grant !== undefined) {
    const grant = parseWireGrant(body.credential_grant);
    if (!grant) {
      return {
        ok: false,
        message:
          "credential_grant must be {kind:'api_key',api_key} or {kind:'oauth',oauth_token}",
      };
    }
    input.credential_grant = grant;
  }
  return { ok: true, input };
}

/** The plan-first sidecar (x-leaf-approval-policy). Unlike the instant
 * assignment — DATA that must be trusted, hence its authentication throw —
 * this header can only NARROW: present and equal to "plan_first", the runner
 * empties its per-turn auto-approval; absent, malformed or anything else is
 * today's behavior. Ignoring rather than throwing keeps auth-off local demos
 * working, and an attacker who can set harness headers gains nothing (absence
 * is already the widest state). */
export function planFirstOption(req: IncomingMessage): { planFirst?: boolean } {
  return req.headers["x-leaf-approval-policy"] === "plan_first"
    ? { planFirst: true }
    : {};
}

function instantTurnOptions(req: IncomingMessage, authenticated: boolean): Pick<Extract<ConverseTurnValidation, { ok: true }>, "instantAssignment" | "instantDrawingContext"> {
  const encoded = req.headers["x-leaf-instant-assignment"];
  if (typeof encoded !== "string" || !encoded) return {};
  if (!authenticated) throw new AuthorLoopError("instant assignment requires harness caller authentication", 400);
  if (encoded.length > 16_384) throw new AuthorLoopError("instant assignment header is too large", 400);
  let value: unknown;
  try {
    value = JSON.parse(Buffer.from(encoded, "base64url").toString("utf8"));
  } catch {
    throw new AuthorLoopError("instant assignment header is malformed", 400);
  }
  if (!isRecord(value) || !isRecord(value.drawing_context)) {
    throw new AuthorLoopError("instant assignment header is invalid", 400);
  }
  const instantAssignment = value as unknown as InstantSessionAssignment;
  return { instantAssignment, instantDrawingContext: instantAssignment.drawing_context };
}

/**
 * Drive one converse turn to completion, streaming `application/x-ndjson`.
 *
 * Ordering is the load-bearing part: the FIRST event is pulled BEFORE
 * `res.writeHead` is called. If the runner rejects synchronously or on that
 * first yield — e.g. `GrantRequiredError` (missing per-tenant Claude grant) —
 * the rejection propagates out of this function (it is NOT caught here) so the
 * caller's outer try/catch renders the same non-stream 401 `{grant_required:true}`
 * shape /author uses (server.ts:258-263). The caller therefore never observes a
 * half-open 200 stream for a turn that never started.
 *
 * Once streaming has begun, a mid-stream throw from the runner is caught here
 * (headers are already committed) and rendered as an in-band `error` event
 * followed by a `turn_complete{stop_reason:'error'}` event, matching the FROZEN
 * invariant that every stream ends with one of those two event types.
 *
 * A client disconnect (`res` 'close' - the response socket, see below) does TWO
 * things, in order: (1) fires the AbortSignal handed to `runTurn` — because
 * `iterator.return()` alone merely QUEUES behind a pending `next()` per the
 * async-generator spec, so a hung/expensive first SDK call would keep running
 * until it settled on its own; the signal preempts that pull by aborting the
 * runner's underlying work (the SDK's AbortController) immediately — and then
 * (2) calls `iterator.return()` so the generator unwinds through `finally` and
 * no further chunks are written.
 */
async function streamTurn(
  _req: IncomingMessage,
  res: ServerResponse,
  runner: ConverseRunner,
  input: ConverseTurnInput,
  instant?: Pick<Extract<ConverseTurnValidation, { ok: true }>, "instantAssignment" | "instantDrawingContext"> & { planFirst?: boolean },
): Promise<void> {
  const turnAbort = new AbortController();
  const iterator = runner.runTurn(input, { signal: turnAbort.signal, ...instant })[Symbol.asyncIterator]();

  let closed = false;
  const onClose = (): void => {
    closed = true;
    turnAbort.abort();
    const ret = iterator.return?.(undefined);
    if (ret && typeof (ret as Promise<unknown>).catch === "function") {
      void (ret as Promise<unknown>).catch(() => {});
    }
  };
  // Installed on the RESPONSE socket, and BEFORE the first pull. `req` has already been
  // fully drained by readJsonBody() by the time we get here, so a 'close' listener on it
  // can miss an early disconnect; and a disconnect that happens WHILE we are still
  // awaiting the runner's FIRST event (e.g. mid an LLM call, before any response bytes
  // have gone out) must be observed the moment it fires - EventEmitter drops events
  // emitted before a listener is attached, so registering this after that first pull (as
  // before) silently misses exactly that case.
  res.on("close", onClose);
  // A write/writeHead issued after the peer has already closed the connection must not
  // surface as an unhandled 'error' event (process crash) - onClose above already stops
  // both writing and pulling; this just keeps the EventEmitter contract safe either way.
  res.on("error", () => {});

  // Pull the first event BEFORE writing the response head (see doc comment above).
  let step: IteratorResult<HarnessTurnEvent>;
  try {
    step = await iterator.next();
  } catch (err) {
    res.off("close", onClose);
    if (closed) {
      // Our own disconnect-triggered abort surfaced as a rejection (e.g. the SDK's
      // AbortError). The peer is gone — there is no response left to shape.
      res.end();
      return;
    }
    // Genuine pre-stream failure (GrantRequiredError etc.): propagate so the caller
    // renders the non-stream 401/error shape, exactly as before.
    throw err;
  }

  res.writeHead(200, { "content-type": "application/x-ndjson" });

  try {
    while (!step.done) {
      if (closed) break;
      const ev = step.value as HarnessTurnEvent;
      res.write(`${JSON.stringify(ev)}\n`);
      step = await iterator.next();
    }
  } catch (err) {
    if (!closed) {
      const message = err instanceof Error ? err.message : String(err);
      res.write(`${JSON.stringify({ type: "error", data: { message } })}\n`);
      res.write(`${JSON.stringify({ type: "turn_complete", data: { stop_reason: "error" } })}\n`);
    }
  } finally {
    res.off("close", onClose);
    res.end();
  }
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
    const requestUrl = new URL(req.url ?? "/", "http://leaf-harness.internal");
    const path = requestUrl.pathname;
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
        const grantSuffix = path.slice("/grants/".length);
        const diagnosticRequest = grantSuffix.endsWith("/diagnostic");
        const encodedTenant = diagnosticRequest
          ? grantSuffix.slice(0, -"/diagnostic".length)
          : grantSuffix;
        const tenantId = decodeURIComponent(encodedTenant);
        if (!tenantId) return send(res, 400, { error: { message: "tenant id required" } });
        if (!ports.grantAdmin) {
          return send(res, 501, { error: { message: "grant admin store not configured" } });
        }
        try {
          if (diagnosticRequest) {
            if (method !== "GET") {
              return send(res, 405, { error: { message: `method ${method} not allowed on ${path}` } });
            }
            if (!ports.grantAdmin.diagnostic) {
              return send(res, 501, { error: { message: "grant diagnostics not configured" } });
            }
            return send(res, 200, await ports.grantAdmin.diagnostic(tenantId));
          }
          if (method === "PUT") {
            const gbody = await readJsonBody(req);
            const token = typeof gbody.token === "string" ? gbody.token : "";
            if (!token.trim()) return send(res, 400, { error: { message: "token is required" } });
            // Optional explicit kind; when absent/invalid the store AUTO-DETECTS from the
            // token prefix (§17). Only the two valid literals are forwarded.
            const rawKind = typeof gbody.kind === "string" ? gbody.kind.trim() : "";
            const kind = rawKind === "oauth" || rawKind === "api_key" ? rawKind : undefined;
            const label = typeof gbody.label === "string" ? gbody.label : undefined;
            const rawPlan = typeof gbody.plan === "string" ? gbody.plan.trim().toLowerCase() : "";
            const plan = rawPlan === "pro" || rawPlan === "max" ||
              rawPlan === "team" || rawPlan === "enterprise" ? rawPlan : undefined;
            const st = await ports.grantAdmin.put(tenantId, token, kind, label, plan);
            return send(res, 200, st); // {linked, linked_at, kind} — token never echoed
          }
          if (method === "PATCH") {
            const gbody = await readJsonBody(req);
            const accountId = typeof gbody.account_id === "string" ? gbody.account_id.trim() : "";
            if (!accountId) return send(res, 400, { error: { message: "account_id is required" } });
            return send(res, 200, await ports.grantAdmin.activate(tenantId, accountId));
          }
          if (method === "GET") {
            return send(res, 200, await ports.grantAdmin.status(tenantId));
          }
          if (method === "DELETE") {
            const accountId = requestUrl.searchParams.get("account_id")?.trim() || undefined;
            await ports.grantAdmin.remove(tenantId, accountId);
            // Honest post-remove state (sol-critic F2): the demo tenant's documented
            // §16 env/file fallback can still be active after the per-tenant files are
            // gone — report the store's actual status, never a hardcoded linked:false.
            return send(res, 200, await ports.grantAdmin.status(tenantId));
          }
        } catch (e) {
          // e.g. a malformed/traversal tenant id — a client error, never a token leak.
          return send(res, 400, { error: { message: (e as Error).message } });
        }
        return send(res, 405, { error: { message: `method ${method} not allowed on ${path}` } });
      }

      if (method === "POST" && path === "/author") {
        if (!authoredExecutionEnabled()) {
          return send(res, 403, {
            error: { message: "tenant-authored execution is disabled" },
          });
        }
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        const description = typeof body.description === "string" ? body.description : "";
        if (!description.trim()) {
          return send(res, 400, { error: { message: "description is required" } });
        }
        // A live lifecycle has an isolated stage/publish path. Never let the
        // compatibility route move main when that path is wired.
        if (auth.enabled && ports.customizationCoordination) {
          return send(res, 410, {
            error: { message: "live authoring requires /author/stage then /author/publish" },
          });
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

      if (method === "POST" && path === "/author/stage") {
        if (!authoredExecutionEnabled()) {
          return send(res, 403, {
            error: { message: "tenant-authored execution is disabled" },
          });
        }
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        const description = requiredText(body, "description");
        // These names deliberately match AuthorLoop.stage's request exactly.
        const out = await loop.stage(tenant, description, {
          changeSetId: requiredText(body, "changeSetId"),
          expectedBaseSha: requiredText(body, "expectedBaseSha"),
          platformRelease: requiredText(body, "platformRelease"),
          workspaceContractDigest: requiredText(body, "workspaceContractDigest"),
          idempotencyKey: requiredText(body, "idempotencyKey"),
        });
        return send(res, 200, out);
      }

      if (method === "POST" && path === "/author/repository") {
        if (!authoredExecutionEnabled()) {
          return send(res, 403, {
            error: { message: "tenant-authored execution is disabled" },
          });
        }
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        return send(res, 200, await loop.ensureRepository(tenant));
      }

      if (method === "POST" && path === "/author/publish") {
        if (!authoredExecutionEnabled()) {
          return send(res, 403, {
            error: { message: "tenant-authored execution is disabled" },
          });
        }
        const body = await readJsonBody(req);
        const receipt = body.receipt;
        if (!receipt || typeof receipt !== "object" || Array.isArray(receipt)) {
          return send(res, 400, { error: { message: "receipt is required" } });
        }
        // Do not reconstitute receipt fields. The exact staged receipt is the
        // publish authority and AuthorLoop verifies its private ref again.
        const out = await loop.publish(
          receipt as import("./ports/index.js").StagedCustomizationReceipt,
          requiredText(body, "expectedMainSha"),
        );
        return send(res, 200, out);
      }

      if (method === "POST" && path === "/turn") {
        const body = await readJsonBody(req);
        const validated = validateConverseTurnInput(body);
        if (!validated.ok) {
          return send(res, 400, { error: { message: validated.message } });
        }
        if (!ports.converseRunner) {
          return send(res, 501, {
            error: { message: "converse runner not configured", code: "not_implemented" },
          });
        }
        // Awaited (not `return`ed bare) so a rejection — e.g. GrantRequiredError on the
        // first event — is caught by THIS function's own try/catch below, not silently
        // dropped: async-function `return somePromise` does not route through a local
        // catch, only `await` does.
        await streamTurn(req, res, ports.converseRunner, validated.input,
          { ...instantTurnOptions(req, auth.enabled), ...planFirstOption(req) });
        return;
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
      // Diagnostic: full stack to stderr, token-redacted (defense in depth — no
      // designed path puts a grant in an error message, but a thrown string is
      // arbitrary and this line must never be the leak).
      console.error("[harness] request error:", redactTokens((err as Error).stack ?? String(err)));
      // Missing per-tenant grant -> a clean 401 with a machine-detectable marker the app
      // proxy maps to GRANT_REQUIRED (frontend prompts "sign in with Claude"). No token.
      if (err instanceof GrantRequiredError) {
        return send(res, 401, {
          grant_required: true,
          error: { message: err.message, code: "grant_required" },
        });
      }
      if (err instanceof GrantPoolUnavailableError) {
        return send(res, 429, {
          errorCode: "llm_quota_exhausted",
          message: err.message,
          retry_after_s: err.retryAfterS,
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
type ProductionInstantExecutorClientOptions = {
  requireTls: true;
  caFile: string;
  clientCertificateFile: string;
  clientPrivateKeyFile: string;
  tlsServerName: string;
};

type ProductionInstantExecutorClientFactory<T> = (options: ProductionInstantExecutorClientOptions) => T;

const DIRECT_EXECUTOR_FILE_ENV = [
  "LEAF_INSTANT_EXECUTOR_CA_FILE",
  "LEAF_INSTANT_EXECUTOR_CERT_FILE",
  "LEAF_INSTANT_EXECUTOR_KEY_FILE",
] as const;

const RAW_EXECUTOR_CREDENTIAL_ENV = [
  "LEAF_INSTANT_EXECUTOR_CA",
  "LEAF_INSTANT_EXECUTOR_CERT",
  "LEAF_INSTANT_EXECUTOR_KEY",
  "LEAF_INSTANT_EXECUTOR_CA_PEM",
  "LEAF_INSTANT_EXECUTOR_CLIENT_CERT_PEM",
  "LEAF_INSTANT_EXECUTOR_CLIENT_KEY_PEM",
] as const;

const EXECUTOR_PROXY_ENV = [
  "LEAF_INSTANT_EXECUTOR_PROXY_URL",
  "LEAF_INSTANT_EXECUTOR_PROXY_SECRET",
  "LEAF_INSTANT_EXECUTOR_PROXY_SECRET_FILE",
] as const;

const EXPECTED_EXECUTOR_TLS_SERVER_NAME = "executor.instant.internal";

function requiredEnvironmentFile(env: NodeJS.ProcessEnv, name: string): string {
  const path = env[name]?.trim();
  if (!path) throw new Error(`${name} is required and must be a mounted file path`);
  return path;
}

/**
 * Production uses the executor endpoint in the opaque assignment directly. The
 * TLS material stays in the reusable HTTPS agent constructed by this client.
 * Do not accept raw PEM, or a proxy hop, in the harness process.
 */
export function createProductionInstantExecutorClient<T>(
  factory: ProductionInstantExecutorClientFactory<T>,
  env: NodeJS.ProcessEnv = process.env,
): T {
  const rawCredential = RAW_EXECUTOR_CREDENTIAL_ENV.find((name) => env[name] !== undefined);
  if (rawCredential) {
    throw new Error(`${rawCredential} is not allowed; use mounted executor credential files`);
  }
  const proxySetting = EXECUTOR_PROXY_ENV.find((name) => env[name] !== undefined);
  if (proxySetting) {
    throw new Error(`${proxySetting} is not allowed; the harness connects to the assigned executor directly`);
  }

  const [caFile, clientCertificateFile, clientPrivateKeyFile] = DIRECT_EXECUTOR_FILE_ENV.map(
    (name) => requiredEnvironmentFile(env, name),
  );
  const tlsServerName = requiredEnvironmentFile(env, "LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME");
  if (tlsServerName !== EXPECTED_EXECUTOR_TLS_SERVER_NAME) {
    throw new Error(
      `LEAF_INSTANT_EXECUTOR_TLS_SERVER_NAME must be ${EXPECTED_EXECUTOR_TLS_SERVER_NAME}`,
    );
  }
  return factory({
    requireTls: true,
    caFile,
    clientCertificateFile,
    clientPrivateKeyFile,
    tlsServerName,
  });
}

export async function startReal(port = 8130): Promise<Server> {
  const { AgentSdkRunner } = await import("./ports/impl/agentSdkRunner.js");
  const { BrokerApsClientHttp } = await import("./ports/impl/brokerApsClient.js");
  const { createTenantGrantStore, OAuthGrantProviderImpl } = await import("./ports/impl/oauthGrantProvider.js");
  const { TenantRepoProviderImpl } = await import("./ports/impl/tenantRepoProvider.js");
  const { CustomizationCoordinationClient } = await import("./ports/impl/customizationCoordinationClient.js");
  const { SpineTurnAdapter } = await import("./agent/spineTurnAdapter.js");
  const { HttpAppRunClient } = await import("./ports/impl/appRunClient.js");
  const { HttpGateClient } = await import("./ports/impl/gateClient.js");
  const { ConverseSdkRunner } = await import("./ports/impl/converseSdkRunner.js");
  const { createSessionStore } = await import("./ports/impl/sessionStoreFactory.js");
  const { HttpInstantExecutorClient } = await import("./ports/impl/instantExecutorClient.js");

  const tenantsDir = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
  const tenantGitDir = process.env.LEAF_TENANT_GIT_DIR ?? `${tenantsDir}/tenant-git`;
  const appUrl = (process.env.LEAF_APP_URL ?? "").trim();
  const dispatchSecret = (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim();
  const instantExecutionEnabled = envFlagOn(process.env.LEAF_INSTANT_EXECUTION_ENABLED);
  const instantExecutor = instantExecutionEnabled
    ? createProductionInstantExecutorClient(
        (options) => new HttpInstantExecutorClient(options),
      )
    : undefined;
  // F18 seam: per-tenant grant + admin (one store); LEAF_GRANT_STORE=vault fails loudly.
  const grantStore = createTenantGrantStore();
  const sessionStore = appUrl && dispatchSecret ? createSessionStore() : null;
  const converseRunner = sessionStore
    ? new SpineTurnAdapter({
        oauth: new OAuthGrantProviderImpl({ store: grantStore }),
        appRun: new HttpAppRunClient({ baseUrl: appUrl, dispatchSecret }),
        gate: new HttpGateClient({ appBaseUrl: appUrl, dispatchSecret }),
        store: sessionStore.store,
        runnerFor: (grant) => new ConverseSdkRunner({ grant }),
        instantExecutor,
      })
    : undefined;

  const ports: HarnessPorts = {
    agentRunner: new AgentSdkRunner(),
    broker: new BrokerApsClientHttp(),
    oauth: new OAuthGrantProviderImpl({ store: grantStore }),
    grantAdmin: grantStore,
    tenantRepo: new TenantRepoProviderImpl({
      locator: { async repoRef(t) { return `${tenantsDir}/${t}`; } },
      inPlace: true,
      bareBase: tenantGitDir,
    }),
    ...(appUrl && dispatchSecret
      ? { customizationCoordination: new CustomizationCoordinationClient({ baseUrl: appUrl, dispatchSecret }) }
      : {}),
    ...(converseRunner ? { converseRunner } : {}),
  };
  const server = createHarness(ports).listen(port);
  if (sessionStore) {
    server.once("close", () => { void sessionStore.close(); });
  }
  if (instantExecutor) {
    server.once("close", () => { instantExecutor.close(); });
  }
  return server;
}
