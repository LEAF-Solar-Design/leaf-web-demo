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
import {
  withAuthorStandardServicesAuthority,
  type HumanApprovalHostInput,
  type LeafStandardServicesHumanApprovalHost,
} from "./ports/impl/leafStandardServicesResolver.js";
import {
  dispatchOperatorWorkerJob,
  OperatorWorkerDispatchError,
  type OperatorWorkerDispatchRequest,
} from "./operatorWorker/operatorWorkerDispatch.js";
import type { OperatorWorkerManager } from "./operatorWorker/workerManager.js";

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

function authorityHeader(req: IncomingMessage, name: string): string | undefined {
  const raw = req.headers[name];
  const value = Array.isArray(raw) ? raw[0] : raw;
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function authorAuthorityForRequest(
  req: IncomingMessage,
  tenantId: string,
): Omit<import("./ports/impl/leafStandardServicesResolver.js").AuthorAuthority, "subscription_mount_id"> | undefined {
  const authoritySessionId = authorityHeader(req, "x-authority-session-id");
  const authorityTurnId = authorityHeader(req, "x-authority-turn-id");
  const required = Boolean((process.env.LEAF_TENANT_MCP_BROKER_URL ?? "").trim());
  if (Boolean(authoritySessionId) !== Boolean(authorityTurnId) || (required && !authoritySessionId)) {
    throw new AuthorLoopError("active author authority headers are required", 409);
  }
  if (!authoritySessionId || !authorityTurnId) return undefined;
  return {
    tenant_id: tenantId,
    session_id: authoritySessionId,
    authority_session_id: authoritySessionId,
    authority_turn_id: authorityTurnId,
  };
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

/**
 * Body ceilings. A size check that runs AFTER the body is buffered is not a
 * size check at all: the memory is already spent, and a chunked request
 * declares no length to check up front. So the ceiling is enforced per chunk.
 *
 * Two values because the routes are not alike.
 *
 * `/turn` is sized against what the APP can legitimately forward, which is more
 * than what the app accepts from the browser:
 *
 * comfortably above the app's own 1,500,000-byte inbound cap, with room for the
 * prior context, ids, credential grant and framing the app adds afterwards.
 *
 * The app MEASURES its encoded body against this same number
 * (turn_runner.MAX_FORWARD_BYTES) and trims prior context until it fits, so
 * this is an upper bound on what the app sends rather than a figure arrived at
 * by arithmetic. That matters because arithmetic over decoded text kept being
 * wrong in ways that only showed up on real input: prior context was
 * count-bounded rather than byte-bounded; `requests`' default encoder escapes
 * non-ASCII to \uXXXX and turned one emoji into 12 bytes; and even after
 * byte-bounding, JSON escapes `"` to two bytes and a control character to six,
 * so a budget over decoded text is not a budget over the wire. Change this
 * number and change MAX_FORWARD_BYTES with it.
 *
 * Every other route gets a generous default, because a customization publish
 * carries a staged receipt and a registered run carries arbitrary tool params:
 * bounding those to the IMAGE budget would refuse legitimate payloads that have
 * nothing to do with images.
 */
const MAX_TURN_BODY_BYTES = 2_000_000;
const MAX_BODY_BYTES = 8 * 1024 * 1024;
/** How long one peer may hold a socket while sending (node's default is 300s). */
const REQUEST_TIMEOUT_MS = 120_000;

function readJsonBody(
  req: IncomingMessage,
  maxBytes: number = MAX_BODY_BYTES,
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let seen = 0;
    let refused = false;
    const tooLarge = (): AuthorLoopError =>
      new AuthorLoopError("request body exceeds the harness cap", 413);

    // Over the cap: STOP BUFFERING, keep reading, and answer at `end`.
    //
    // Memory is what the ceiling defends, and discarding costs none of it. The
    // reason to keep reading at all is that WHEN the 413 is written decides
    // whether the client ever reads it: the response carries
    // `connection: close`, node half-closes as soon as it is written, and a
    // peer still uploading gets a broken pipe and reports a network failure
    // instead of the envelope. That reset is not cosmetic — the app maps a
    // dropped connection to BROKER_UNREACHABLE, so the caller is told the
    // harness is down when it actually answered "your payload is too big".
    //
    // Two smarter-looking versions of this were both worse. Destroying the
    // socket at a byte allowance reset every oversized DECLARED request on the
    // 8 MiB routes, because refusal there happens before the first byte and the
    // whole body counts against the allowance. Answering early and pausing
    // deadlocked: node will not finish a response whose request nobody is
    // reading, so the socket never closed at all.
    //
    // What bounds this is not a byte budget but `requestTimeout` on the server
    // (see `listen`), which is the honest control for "a peer is holding a
    // socket too long" and applies to every request, not just refused ones.
    const refuse = (): void => {
      if (refused) return;
      refused = true;
      chunks.length = 0;
    };

    const declared = Number(req.headers["content-length"]);
    if (Number.isFinite(declared) && declared > maxBytes) refuse();

    req.on("data", (c: Buffer) => {
      if (refused) return;                 // read and drop; nothing accumulates
      seen += c.length;
      // Measured as it arrives, not afterwards: a chunked body declares no
      // length, so buffering first and checking later defeats the point.
      if (seen > maxBytes) return refuse();
      chunks.push(c);
    });
    req.on("end", () => {
      // The body has finished arriving, so closing the connection now cannot
      // break the peer's write side — this is the point at which a 413 is
      // actually readable by the client that caused it.
      if (refused) return reject(tooLarge());
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
  const headers: Record<string, string> = { "content-type": "application/json" };
  // 413 always closes. The request that caused it was read but never used, and
  // a refused request is not a good candidate for connection reuse. readJsonBody
  // reads it through to `end` precisely so that closing here cannot break a peer
  // that is still writing.
  if (status === 413) headers.connection = "close";
  res.writeHead(status, headers);
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

const IMAGE_MEDIA_TYPES = new Set(["image/png", "image/jpeg", "image/webp", "image/gif"]);
const MAX_IMAGES_PER_MESSAGE = 3;
const MAX_IMAGE_BYTES = 1024 * 1024;
const MAX_IMAGES_BYTES = 1024 * 1024;

/**
 * Signatures matched in FULL, and matched the same way the app matches them
 * (server/routers/sessions.py `_image_magic_matches`). Two independent checks
 * are the point — the harness does not trust that the app validated — but if
 * they disagree the app accepts a request the harness then rejects, which the
 * user experiences as an unexplained failure after a successful upload.
 */
function imageMagicMatches(mediaType: string, bytes: Buffer): boolean {
  if (mediaType === "image/png") {
    return bytes.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]));
  }
  if (mediaType === "image/jpeg") {
    // SOI plus the first marker, and that is the whole signature. JPEG has no
    // fixed tail — real encoders emit bytes after EOI and decoders read them
    // fine — so requiring EOI last turns away genuine photos.
    return bytes.subarray(0, 3).equals(Buffer.from([0xff, 0xd8, 0xff]));
  }
  if (mediaType === "image/gif") {
    const header = bytes.subarray(0, 6).toString("ascii");
    return header === "GIF87a" || header === "GIF89a";
  }
  if (bytes.length < 12) return false;
  return bytes.subarray(0, 4).toString("ascii") === "RIFF"
    && bytes.subarray(8, 12).toString("ascii") === "WEBP"
    && bytes.readUInt32LE(4) === bytes.length - 8;
}

function validateImages(value: unknown): { ok: true; images: NonNullable<ConverseTurnInput["images"]> } | { ok: false; message: string } {
  if (!Array.isArray(value)) return { ok: false, message: "images must be an array" };
  if (value.length > MAX_IMAGES_PER_MESSAGE) return { ok: false, message: `at most ${MAX_IMAGES_PER_MESSAGE} images are allowed per message` };
  let total = 0;
  const images: NonNullable<ConverseTurnInput["images"]> = [];
  for (const [offset, raw] of value.entries()) {
    const index = offset + 1;
    if (!isRecord(raw) || !IMAGE_MEDIA_TYPES.has(raw.media_type as string)) {
      return { ok: false, message: `image ${index} media_type must be png, jpeg, webp, or gif` };
    }
    if (typeof raw.data !== "string" || !raw.data) return { ok: false, message: `image ${index} data must be non-empty base64` };
    if (!/^[A-Za-z0-9+/]*={0,2}$/.test(raw.data) || raw.data.length % 4 !== 0) {
      return { ok: false, message: `image ${index} data must be valid base64` };
    }
    const bytes = Buffer.from(raw.data, "base64");
    if (!bytes.length) return { ok: false, message: `image ${index} data must be non-empty base64` };
    if (bytes.length > MAX_IMAGE_BYTES) return { ok: false, message: "each image must be at most 1MB decoded" };
    total += bytes.length;
    if (total > MAX_IMAGES_BYTES) return { ok: false, message: "images must total at most 1MB decoded" };
    const media_type = raw.media_type as string;
    if (!imageMagicMatches(media_type, bytes)) return { ok: false, message: `image ${index} media_type does not match its bytes` };
    images.push({ media_type, data: raw.data });
  }
  return { ok: true, images };
}

/**
 * Minimal validation of ConverseTurnInput: the four required id fields, and
 * exactly one of (`text` and/or `images`) | `confirm` to drive the turn.
 * Deliberately does not validate deeper into `confirm.proposal` — that is the
 * runner's job. `messages`
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
  let images: NonNullable<ConverseTurnInput["images"]> | undefined;
  if (body.images !== undefined) {
    const validated = validateImages(body.images);
    if (!validated.ok) return validated;
    images = validated.images;
  }
  // A turn is a user message (text and/or images) OR a confirmation, never both
  // and never neither — the same rule ConverseLoop enforces. Both halves have to
  // be checked HERE, at the boundary, because the adapter acquires a grant and
  // writes the confirmation mirror before the loop ever sees the body: leaving
  // `text + confirm` to the loop turned a boundary 400 into a post-side-effect
  // 500. Refusing images-with-confirm but not text-with-confirm was the gap.
  if (!hasText && !hasConfirm && !images?.length) {
    return { ok: false, message: "one of text, images, or confirm is required" };
  }
  if (hasConfirm && (hasText || images?.length)) {
    return { ok: false, message: "a turn carries either text/images or confirm, not both" };
  }

  const messages = Array.isArray(body.messages)
    ? (body.messages as ConverseTurnInput["messages"])
    : [];

  const input: ConverseTurnInput = { tenant_id, session_id, turn_id, drawing_id, messages };
  if (hasText) input.text = body.text as string;
  if (images?.length) input.images = images;
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
export function createHarness(ports: HarnessPorts, opts?: {
  auth?: HarnessAuthConfig;
  standardServicesApproval?: {
    dispatchSecret: string;
    host: LeafStandardServicesHumanApprovalHost;
  };
  /** Lane D (O2/O3): the isolated operator worker. Absent -> the dispatch route
   * ships dark (501). The manager itself still fails closed on a non-isolating
   * substrate, so wiring this grants no broad-execution capability by itself. */
  operatorWorker?: { manager: OperatorWorkerManager };
}): Harness {
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

      if (method === "POST" && path.startsWith("/internal/standard-services/approvals/")) {
        const configured = opts?.standardServicesApproval;
        const provided = authorityHeader(req, "x-dispatch-secret") ?? "";
        if (
          !configured?.dispatchSecret
          || !provided
          || !secretsEqual(provided, configured.dispatchSecret)
        ) {
          return send(res, 401, { error: { code: "approval_host_unauthorized", message: "approval host authority is required" } });
        }
        res.setHeader("cache-control", "no-store");
        res.setHeader("pragma", "no-cache");
        const body = await readJsonBody(req, 64 * 1024);
        if (path === "/internal/standard-services/approvals/review") {
          try {
            const reviewed = await configured.host.review({
              approval_id: requiredText(body, "approval_id"),
              argument_digest: requiredText(body, "argument_digest"),
              tenant_id: requiredText(body, "tenant_id"),
              subject_id: requiredText(body, "subject_id"),
            });
            if (!reviewed) {
              return send(res, 404, { error: { code: "approval_unavailable", message: "approval is unavailable" } });
            }
            const identity = {
              tenant_id: reviewed.identity.tenant_id,
              subject_id: reviewed.identity.subject_id,
              session_id: reviewed.identity.session_id,
              authority_turn_id: reviewed.identity.authority_turn_id,
              subscription_mount_id: reviewed.identity.subscription_mount_id,
              runner_profile_id: reviewed.identity.runner_profile_id,
            };
            if (reviewed.status === "completed") {
              if (typeof reviewed.receipt_id !== "string" || !/^[a-f0-9]{64}$/.test(reviewed.receipt_id)) {
                throw new Error("invalid approval receipt");
              }
              return send(res, 200, {
                status: "completed",
                identity,
                receipt_id: reviewed.receipt_id,
                ...(reviewed.artifact_ids?.length ? { artifact_ids: [...reviewed.artifact_ids] } : {}),
              });
            }
            return send(res, 200, { status: reviewed.status, identity });
          } catch {
            return send(res, 409, { error: { code: "approval_unavailable", message: "approval is unavailable" } });
          }
        }
        if (path === "/internal/standard-services/approvals/execute") {
          try {
            const receipt = await configured.host.execute(body as unknown as HumanApprovalHostInput);
            if (receipt.status === "uncertain") return send(res, 200, { status: "uncertain" });
            if (typeof receipt.receipt_id !== "string" || !/^[a-f0-9]{64}$/.test(receipt.receipt_id)) {
              throw new Error("invalid approval receipt");
            }
            return send(res, 200, {
              status: "completed",
              receipt_id: receipt.receipt_id,
              ...(receipt.artifact_ids?.length ? { artifact_ids: [...receipt.artifact_ids] } : {}),
            });
          } catch {
            return send(res, 409, { error: { code: "approval_unavailable", message: "approval is unavailable" } });
          }
        }
        return send(res, 404, { error: { code: "not_found", message: "route not found" } });
      }

      // F5 caller-auth gate: every non-health route requires the shared secret when the
      // gate is enabled. Rejected BEFORE any body read, tenant resolution, or store touch,
      // so an unauthed caller can neither author, run, nor read/overwrite/delete a grant.
      const auth = explicitAuth ?? resolveHarnessAuth();
      const denial = harnessAuthDenial(req, auth);
      if (denial) return send(res, 401, denial);

      // Operator worker dispatch (contract/OPERATOR.md Lane D, O2/O3 wiring): the
      // app's capability-only handler forwards a BOUNDED job over this same
      // secret-gated hop; the job runs ONLY in the isolated disposable worker.
      // Ships dark: 501 until a manager (with a real isolating substrate) is wired.
      if (method === "POST" && path === "/operator/worker/dispatch") {
        if (!opts?.operatorWorker) {
          return send(res, 501, {
            error: { message: "operator worker not configured", code: "not_implemented" },
          });
        }
        const body = await readJsonBody(req);
        const request: OperatorWorkerDispatchRequest = {
          commands: Array.isArray(body.commands)
            ? (body.commands as unknown[]).filter((c): c is string => typeof c === "string")
            : [],
          repo: typeof body.repo === "string" ? body.repo : undefined,
          idempotencyKey: typeof body.idempotencyKey === "string" ? body.idempotencyKey : "",
          principalSubject: typeof body.principalSubject === "string" ? body.principalSubject : "",
          sessionId: typeof body.sessionId === "string" ? body.sessionId : "",
          timeoutMs: typeof body.timeoutMs === "number" ? body.timeoutMs : undefined,
        };
        try {
          const receipt = await dispatchOperatorWorkerJob(opts.operatorWorker.manager, request);
          return send(res, 200, receipt);
        } catch (err) {
          const reason = err instanceof Error ? err.message : String(err);
          if (reason === "substrate_not_isolating") {
            // Isolation refusal is a service posture, not a caller error: the
            // worker exists but will not run broad work un-jailed.
            return send(res, 503, {
              error: { code: reason, message: "operator worker refused: substrate not isolating" },
            });
          }
          if (err instanceof OperatorWorkerDispatchError
            || reason === "workspace_invalid" || reason === "commands_invalid"
            || reason === "idempotency_key_required" || reason === "principal_required") {
            return send(res, 400, {
              error: { code: reason, message: "operator worker dispatch rejected" },
            });
          }
          throw err;
        }
      }

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
        const authority = authorAuthorityForRequest(req, tenant);
        // These names deliberately match AuthorLoop.stage's request exactly.
        const stage = () => loop.stage(tenant, description, {
            changeSetId: requiredText(body, "changeSetId"),
            expectedBaseSha: requiredText(body, "expectedBaseSha"),
            platformRelease: requiredText(body, "platformRelease"),
            workspaceContractDigest: requiredText(body, "workspaceContractDigest"),
            idempotencyKey: requiredText(body, "idempotencyKey"),
            ...(body.targetToolName === undefined
              ? {}
              : { targetToolName: requiredText(body, "targetToolName") }),
          });
        const out = authority
          ? await withAuthorStandardServicesAuthority(authority, stage)
          : await stage();
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

      if (method === "POST" && path === "/author/remove") {
        if (!authoredExecutionEnabled()) {
          return send(res, 403, {
            error: { message: "tenant-authored execution is disabled" },
          });
        }
        const body = await readJsonBody(req);
        const tenant = tenantForRequest(req, body);
        const out = await loop.stageRemoval(tenant, {
          toolName: requiredText(body, "toolName"),
          expectedCatalogDigest: requiredText(body, "expectedCatalogDigest"),
          changeSetId: requiredText(body, "changeSetId"),
          expectedBaseSha: requiredText(body, "expectedBaseSha"),
          platformRelease: requiredText(body, "platformRelease"),
          workspaceContractDigest: requiredText(body, "workspaceContractDigest"),
          idempotencyKey: requiredText(body, "idempotencyKey"),
        });
        return send(res, 200, out);
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
        const body = await readJsonBody(req, MAX_TURN_BODY_BYTES);
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
      // How long any one peer may hold a socket while sending. This is the
      // bound on an over-cap body being read and discarded, and on a slow
      // writer generally — node's default is 300s, which is a long time to
      // occupy a connection for a request already known to be refused. 8 MiB
      // in 120s is 70 KB/s, far below anything a real client does.
      server.requestTimeout = REQUEST_TIMEOUT_MS;
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

export function createEnabledProductionInstantExecutorClient<T>(
  factory: ProductionInstantExecutorClientFactory<T>,
  env: NodeJS.ProcessEnv = process.env,
): T | undefined {
  return envFlagOn(env.LEAF_INSTANT_EXECUTION_ENABLED)
    ? createProductionInstantExecutorClient(factory, env)
    : undefined;
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
  const { HaikuIntentSynthesizer } = await import(
    "./ports/impl/haikuIntentSynthesizer.js"
  );
  const { createSessionStore } = await import("./ports/impl/sessionStoreFactory.js");
  const { HttpInstantExecutorClient } = await import("./ports/impl/instantExecutorClient.js");
  const { upstreamSinkFromEnv } = await import("./ports/impl/httpUpstreamSink.js");
  const {
    AuthorStandardServicesRunner,
    StandardServicesOAuthGrantProvider,
    standardServicesResolverFromEnv,
  } = await import("./ports/impl/leafStandardServicesResolver.js");

  const tenantsDir = process.env.LEAF_TENANTS_DIR ?? "C:/tmp/leaf-tenants";
  const tenantGitDir = process.env.LEAF_TENANT_GIT_DIR ?? `${tenantsDir}/tenant-git`;
  const appUrl = (process.env.LEAF_APP_URL ?? "").trim();
  const dispatchSecret = (process.env.LEAF_APP_DISPATCH_SECRET ?? "").trim();
  const instantExecutor = createEnabledProductionInstantExecutorClient(
    (options) => new HttpInstantExecutorClient(options),
  );
  // F18 seam: per-tenant grant + admin (one store); LEAF_GRANT_STORE=vault fails loudly.
  const grantStore = createTenantGrantStore();
  const standardServicesResolver = standardServicesResolverFromEnv();
  const oauth = new StandardServicesOAuthGrantProvider(
    new OAuthGrantProviderImpl({ store: grantStore }),
  );
  const sessionStore = appUrl && dispatchSecret ? createSessionStore() : null;
  const converseRunner = sessionStore
    ? new SpineTurnAdapter({
        oauth,
        appRun: new HttpAppRunClient({ baseUrl: appUrl, dispatchSecret }),
        gate: new HttpGateClient({ appBaseUrl: appUrl, dispatchSecret }),
        store: sessionStore.store,
        runnerFor: (grant) => new ConverseSdkRunner({
          grant,
          ...(standardServicesResolver ? { standardServicesResolver } : {}),
        }),
        ...(standardServicesResolver ? { standardServicesResolver } : {}),
        // Classify the turn's surface (drawing vs the product) with a small
        // fast model instead of matching vocabulary in the prompt. Advisory
        // and fail-open: see HaikuIntentSynthesizer.
        intentFor: (grant) => new HaikuIntentSynthesizer({ grant }),
        instantExecutor,
      })
    : undefined;

  // OPTIONAL platform-improvement capture. Constructed only when
  // UPSTREAM_SINK_URL and UPSTREAM_SINK_TOKEN are BOTH set (see
  // upstreamSinkFromEnv), so with the env absent the port stays unwired and
  // this ports object is byte-identical to what it was before. The sink is
  // fire-and-forget by vendored contract: authoring behavior is unchanged
  // when the queue is slow, down, or unconfigured.
  const upstreamSink = upstreamSinkFromEnv();

  const basePorts = {
    agentRunner: new AuthorStandardServicesRunner(
      new AgentSdkRunner({
        ...(standardServicesResolver ? { standardServicesResolver } : {}),
      }),
      Boolean(standardServicesResolver),
    ),
    broker: new BrokerApsClientHttp(),
    oauth,
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

  // Opting into the sink REQUIRES declaring the model-output posture: the
  // vendored type makes those three inseparable so nobody enables capture and
  // silently inherits an export default they never chose. Both stay FALSE.
  // dangerouslyExportModelOutput=false keeps generated code and raw error text
  // out of the queue (prompt, status and an allowlisted failure category still
  // ship). trustRunnerFailureCategories=false re-derives a runner-supplied
  // category rather than believing it, because any runner can throw model text.
  //
  // Branched rather than spread on purpose: HarnessPorts is a discriminated
  // union, and a conditional spread produces OPTIONAL properties that satisfy
  // neither arm. Two concrete objects type-check; one object with maybe-absent
  // keys does not.
  const ports: HarnessPorts = upstreamSink
    ? {
        ...basePorts,
        upstreamSink,
        dangerouslyExportModelOutput: false,
        trustRunnerFailureCategories: false,
      }
    : basePorts;
  const server = createHarness(ports).listen(port);
  if (sessionStore) {
    server.once("close", () => { void sessionStore.close(); });
  }
  if (instantExecutor) {
    server.once("close", () => { instantExecutor.close(); });
  }
  return server;
}
