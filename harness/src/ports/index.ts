/**
 * Port boundary for the tenant author harness.
 *
 * Four inputs come from sibling lanes that may not exist yet. Each is modeled as
 * a typed PORT with an in-repo FAKE (build + test hermetically now) and a real
 * impl STUB (compiles now; live path operator-gated):
 *
 *   OAuthGrantProvider  - Concern 2: the tenant's Agent SDK auth (Claude OAuth
 *                         grant, or BYO API key). NEVER the Auth0 platform JWT.
 *   TenantRepoProvider  - a checkout of the tenant's mushy-codebase git repo +
 *                         commit().
 *   BrokerApsClient     - run/test-run a tool on APS via the credential broker
 *                         (never raw APS creds). Maps to POST /broker/run.
 *   AgentRunner         - the Agent SDK loop boundary (real = SDK, fake =
 *                         scripted). The ONLY component that ever reaches
 *                         Anthropic, and only on the design-time author path.
 *
 * These types also carry the frozen data contracts (CONTRACT.md sections 2/3 +
 * hot-script SPEC section 7) so every file agrees on one shape.
 */

// --------------------------------------------------------------------------- //
// Frozen data contracts
// --------------------------------------------------------------------------- //

/** JSON Schema fragment (structural — we validate the fields we depend on). */
export interface JsonSchema {
  type?: string;
  properties?: Record<string, unknown>;
  required?: string[];
  [k: string]: unknown;
}

/** Capability effect declarations (CONTRACT section 2 / SPEC section 10.2). */
export type Capability = "drawing.read" | "drawing.write";

/**
 * Tool package = CONTRACT.md section 2 (the registry entry) UNIONED with the
 * hot-script SPEC section 7.1 `tool.json` manifest fields. A single object
 * satisfies both: it validates against CONTRACT section 2 AND carries the
 * SPEC section 7 manifest fields (`entry`, `timeout_ms`, `idempotent`,
 * `review`). Extra fields are permitted by both validators.
 */
export interface ToolPackage {
  // --- CONTRACT section 2 (frozen) ---
  name: string; // kebab-case, unique, = MCP tool suffix
  version: string;
  description: string;
  kind: "script" | "appbundle";
  engine_op: string;
  params: JsonSchema; // JSON Schema
  returns: JsonSchema;
  capabilities: Capability[];
  provenance: ToolProvenance;
  // --- hot-script SPEC section 7.1 tool.json (design-time author metadata) ---
  entry?: string; // entry script, relative to the tool package dir (e.g. "tool.py")
  timeout_ms?: number;
  idempotent?: boolean;
  review?: { status: "unreviewed" | "reviewed" | "rejected" };
}

export interface ToolProvenance {
  author: "agent" | "user";
  created: string; // ISO-8601
  session?: string;
  modified?: string;
  static_scan?: unknown[];
}

/** Registry = { tools: [ <tool package> ] } — written into the TENANT repo. */
export interface Registry {
  tools: ToolPackage[];
}

/** Result envelope = CONTRACT.md section 3, plus ADDENDUM section 10 `degraded_mode`. */
export interface ResultEnvelope {
  ok: boolean;
  tool: string | null;
  version: string;
  result: Record<string, unknown>;
  overlay: ResultOverlay | null;
  timing_ms: number;
  cost: { engine_seconds?: number; usd_est?: number } | null;
  error: { error_code: string; message: string; retryable: boolean } | null;
  degraded_mode?: boolean;
}

export interface ResultOverlay {
  highlight_handles?: string[];
  markers?: { pt: [number, number]; label: string }[];
  polylines?: { pts: [number, number][]; color?: string }[];
}

/**
 * Authoring telemetry (A1) — the real provenance/cost numbers surfaced from ONE
 * design-time author build so the UI can render a telemetry/provenance chip. Every
 * field is OPTIONAL: a runner that cannot measure a dimension omits it, and a runner
 * that meters nothing (the hermetic fake) omits the whole object — so the /author
 * response stays additively `{tool, code, preview}` when telemetry is absent.
 *
 * Populated by AgentSdkRunner from its self-metered `lastRun` summary
 * (research/agentsdk-usage-visibility.md: meter from each response's usage, there is
 * no balance API). Not carried by the fake or the e2b runner (they leave it undefined).
 */
export interface AuthorTelemetry {
  /** SDK turns consumed by the author session. */
  turns?: number;
  /** Prompt (input) tokens across the session. */
  input_tokens?: number;
  /** Completion (output) tokens across the session. */
  output_tokens?: number;
  /** SDK-reported total cost of the author session in USD (omitted when unknown). */
  total_cost_usd?: number;
  /** Distinct model id(s) the author session used. */
  models?: string[];
}

/**
 * Author response = CONTRACT.md section 4: POST /api/author -> {tool, code, preview}.
 * `telemetry` is an ADDITIVE, OPTIONAL extension (A1): present only when the runner
 * metered the build; absent-safe so the frozen 3-field shape is unchanged otherwise.
 */
export interface AuthorResponse {
  tool: ToolPackage;
  code: string;
  preview: string;
  telemetry?: AuthorTelemetry;
}

// --------------------------------------------------------------------------- //
// Port 1 - OAuthGrantProvider (Concern 2 ONLY; never the platform JWT)
// --------------------------------------------------------------------------- //

/**
 * The Agent SDK credential for ONE tenant's own Claude subscription (web lane,
 * per-user OAuth) or a BYO API key (enterprise lane).
 *
 * W1 finding (research/agentsdk-usage-visibility.md): subscription OAuth tokens
 * are INDIVIDUAL-USE - one per end user, never shared/pooled. This grant is the
 * user's OWN token, injected explicitly into a scrubbed child env (never read
 * from ambient process env, never logged, never mingled with the tenant JWT).
 */
export type AgentGrant =
  | { kind: "oauth"; oauthToken: string } // consumed as CLAUDE_CODE_OAUTH_TOKEN
  | { kind: "api_key"; apiKey: string }; //  consumed as ANTHROPIC_API_KEY

/** The credential kind of a linked grant (web-lane OAuth vs enterprise BYO API key). */
export type GrantKind = AgentGrant["kind"];

export interface OAuthGrantProvider {
  /** Resolve the per-tenant Agent SDK grant. Concern 2 only. */
  getGrant(tenantId: string): Promise<AgentGrant>;
}

/** The default tenant id when a request carries none (the proven demo loop). */
export const DEFAULT_TENANT = "demo-tenant";

/** Grant link state for a tenant — NEVER carries the token value. */
export interface GrantStatus {
  linked: boolean;
  /** ISO-8601 when the token file was last written, or null (e.g. env-fallback grant). */
  linked_at: string | null;
  /**
   * Which credential kind is linked: `"oauth"` (web-lane per-user "sign in with Claude"
   * token) or `"api_key"` (enterprise BYO key). Present when `linked` is true; omitted
   * when unlinked. NEVER the token value itself.
   */
  kind?: GrantKind;
}

/** Token-free operational facts about one tenant grant record. */
export interface GrantDiagnostic {
  schema: "leaf.grant-diagnostic.v1";
  linked: boolean;
  kind: GrantKind | "missing";
  linked_at: string | null;
  backend: "file";
  path_class: "efs_access_point" | "local_file" | "environment";
  record_format: "v1" | "legacy" | "environment" | "missing" | "invalid";
  legacy_fallback_present: boolean;
  owner: { uid: number | null; gid: number | null; mode: string | null };
  persistence: {
    atomic_publish: boolean;
    file_fsync: boolean;
    directory_fsync: boolean;
  };
  degraded: boolean;
}

/**
 * Admin surface for the per-tenant grant store (wave 4). Backs the harness
 * PUT/GET/DELETE /grants/{tenantId} endpoints so the app can link / check / unlink a
 * tenant's own "sign in with Claude" grant WITHOUT the app ever persisting the token.
 * `status()` and every method return NEVER carry the token value.
 */
export interface TenantGrantAdminStore {
  /**
   * Store (or replace) the tenant's token. `kind` is the credential kind; when omitted
   * it is AUTO-DETECTED from the token prefix (`sk-ant-api…` → api_key; `sk-ant-oat…` →
   * oauth; otherwise oauth). The kind is persisted alongside the token (never logged).
   * Returns the resulting link status (carrying `kind`, never the token).
   */
  put(tenantId: string, token: string, kind?: GrantKind): Promise<GrantStatus>;
  /** Report link status + kind only — never the token. */
  status(tenantId: string): Promise<GrantStatus>;
  /** Remove the tenant's stored token (idempotent). */
  remove(tenantId: string): Promise<void>;
  /** Report token-free storage and ownership facts for operator diagnosis. */
  diagnostic?(tenantId: string): Promise<GrantDiagnostic>;
}

// --------------------------------------------------------------------------- //
// Port 2 - TenantRepoProvider (the tenant's mushy-codebase git repo)
// --------------------------------------------------------------------------- //

export interface HarnessIdentity {
  name: string;
  email: string;
}

/** A checked-out working copy of the tenant's mushy-codebase repo. */
export interface TenantRepo {
  /** Absolute path to the checkout root (registry.json lives here). */
  readonly dir: string;
  /**
   * Stage all changes and create exactly ONE commit authored by the harness
   * identity. Returns the new commit hash.
   */
  commit(message: string, identity: HarnessIdentity): Promise<{ commit: string }>;
}

/** A bare tenant repository used only for isolated customization change sets. */
export interface TenantBareRepo {
  /** Absolute path to a bare Git directory. Never a live checkout. */
  readonly dir: string;
}

/** Execute one short repository operation only while the current writer lease is valid. */
export type TenantMutationFence = <T>(operation: () => T | Promise<T>) => Promise<T>;

export interface TenantRepoProvider {
  /** Provide a checkout of the tenant's repo (git working copy + commit()). */
  checkout(tenantId: string): Promise<TenantRepo>;
  /**
   * Provide a bare repository for the staged customization lifecycle. Legacy
   * providers may omit it, but live stage and publish fail closed without it.
   */
  bare?(tenantId: string): Promise<TenantBareRepo>;
  /** Hold the tenant writer lease and fence every shared Git mutation inside it. */
  withTenantLease?<T>(
    tenantId: string,
    action: (runFenced: TenantMutationFence) => Promise<T>,
  ): Promise<T>;
  /** Read one committed snapshot while excluding a tenant writer. */
  withTenantReadLease?<T>(tenantId: string, action: () => Promise<T>): Promise<T>;
}

/** Immutable fields bound into the leaf.customization.v1 staged receipt. */
export interface StagedCustomizationReceipt {
  readonly contract: "leaf.customization.v1";
  readonly tenant_id: string;
  readonly change_set_id: string;
  readonly state: "staged";
  readonly base_commit: string;
  readonly staged_commit: string;
  readonly catalog_digest: string;
  readonly platform_release: string;
  readonly workspace_contract_digest: string;
  readonly idempotency_key: string;
}

/**
 * Trusted durable coordination boundary. Implementations own change-set state,
 * approval, idempotency, and audit. The model is never granted this port.
 */
export interface CustomizationCoordination {
  recordStaged(receipt: StagedCustomizationReceipt): Promise<void>;
  authorizePublish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<void>;
}

// --------------------------------------------------------------------------- //
// Port 3 - BrokerApsClient (APS execution ONLY through the broker)
// --------------------------------------------------------------------------- //

/** Wire shape mirrors POST /broker/run (CONTRACT-ADDENDUM section 8). */
export interface BrokerRunRequest {
  tenantId: string;
  tool: ToolPackage;
  params: Record<string, unknown>;
  dwg: string;
  apsLive: boolean;
}

export interface BrokerApsClient {
  /** Run (or test-run) a tool on APS via the broker. Returns a section-3 envelope. */
  runTool(req: BrokerRunRequest): Promise<ResultEnvelope>;
}

// --------------------------------------------------------------------------- //
// Port 4 - AgentRunner (the Agent SDK loop boundary)
// --------------------------------------------------------------------------- //

/**
 * The exactly-three tools the design-time author session is granted (mirrors
 * hot-script SPEC section 10: no shell, no arbitrary net). Both the fake and the
 * real SDK runner drive THESE and nothing else.
 */
export interface AuthorToolset {
  /** Read/write scoped to the tenant checkout dir; rejects path escapes. */
  fsTenantRepo: FsTenantRepoTool;
  /** Runs the CONTRACT section 2 oracle; returns pass/fail + diagnostics. */
  validateTool: (tool: ToolPackage) => ValidationResult;
  /** Test-runs a candidate tool via the broker (broker only, aps_live=false). */
  apsTestRun: (tool: ToolPackage, params?: Record<string, unknown>) => Promise<ResultEnvelope>;
}

export interface FsTenantRepoTool {
  readonly root: string;
  readFile(relPath: string): string;
  writeFile(relPath: string, content: string): void;
  exists(relPath: string): boolean;
  listDir(relPath?: string): string[];
}

export interface ValidationResult {
  ok: boolean;
  diagnostics: string[];
}

export interface AgentRunInput {
  description: string;
  systemPrompt: string;
  repoDir: string;
  grant: AgentGrant;
  toolset: AuthorToolset;
}

export interface AgentRunResult {
  /** The authored tool package (already written into the repo by the session). */
  tool: ToolPackage;
  /** The generated entry-script source. */
  code: string;
  /** A short human preview of what the tool does. */
  preview: string;
  /** Files the session wrote, relative to repoDir (for observability/tests). */
  files: string[];
  /**
   * OPTIONAL authoring telemetry (A1): turns/tokens/cost/models for this build.
   * A runner that meters populates it (AgentSdkRunner); one that does not (the fake,
   * the e2b runner) leaves it undefined — so `e2bAgentRunner.ts` compiles unchanged.
   */
  telemetry?: AuthorTelemetry;
}

export interface AgentRunner {
  /** Spawn ONE design-time author session and tear it down. Never on the run path. */
  run(input: AgentRunInput): Promise<AgentRunResult>;
}

// --------------------------------------------------------------------------- //
// Port 5 - ConverseRunner (the sessions/turn-engine loop boundary)
// --------------------------------------------------------------------------- //

import type { ConverseRunner } from "./converse.js";

/**
 * Re-exported from ./converse.js (FROZEN — leaf-backend-gaps.md §2.1): the
 * `POST /turn` NDJSON boundary the turn engine (server/turn_runner.py, S3)
 * drives. Defined in its own module so converse.ts stays a single-purpose,
 * dependency-free port; re-exported here so callers can import everything
 * from `ports/index.js` like the other four ports.
 */
export type { StopReason, HarnessTurnEvent, ConverseTurnInput, ConverseRunner, ConverseRunOptions } from "./converse.js";

// --------------------------------------------------------------------------- //
// Conversational spine ports (section 18 / converse lane — PR #5). PARKED at
// the 2026-07-21 merge resolution: the §2.1 sessions wire above owns the live
// turn path (`converseRunner` in HarnessPorts + POST /turn); the spine's
// model-loop runner below is renamed SpineConverseRunner (`ConverseRunner`
// stays the FROZEN §2.1 name) and its /converse/* server surface is unwired
// pending spine unification. The ConverseLoop PLANS/EXPLAINS/DISPATCHES —
// registered-tool EXECUTION stays on the deterministic job spine (invariant v2,
// enforced by test/converseRuntimeSeparation.test.ts).
// --------------------------------------------------------------------------- //

/** The six spine tool names — the catalog is DATA, not tools, so this set is fixed. */
export const SPINE_TOOL_NAMES = [
  "catalog_search",
  "drawing_state",
  "run_capability",
  "job_status",
  "author_tool",
  "request_confirmation",
] as const;
export type SpineToolName = (typeof SPINE_TOOL_NAMES)[number];

/** Wire event vocabulary (contract addendum section 18 / pinned wire contract section 3). */
export type ConverseEventType =
  | "turn_started"
  | "text_delta"
  | "tool_call"
  | "tool_result"
  | "job_linked"
  | "proposed_run"
  | "confirmation_required"
  | "confirmation_resolved"
  | "turn_usage"
  | "turn_complete"
  | "session_state"
  | "error";

/** turn_complete stop reasons (wire contract section 3, exact strings). */
export type ConverseStopReason =
  | "end_turn"
  | "awaiting_approval"
  | "cap_hit"
  | "llm_quota_exhausted"
  | "llm_rate_limited"
  | "error"
  | "timeout";

/** One SSE event, both hops (wire contract section 3 envelope). */
export interface ConverseEvent {
  v: 1;
  session_id: string;
  turn_id: string;
  seq: number;
  type: ConverseEventType;
  data: Record<string, unknown>;
}

/** turn_usage payload (wire contract section 3; key names exact). */
export interface ConverseTurnUsage {
  turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  /** Cost-relevant tokens = input + output + cache_creation (cache_read excluded). */
  cost_tokens: number;
  total_cost_usd?: number;
  models?: string[];
}

/** The result a spine tool hands back to the model (mirrors MCP CallToolResult). */
export interface SpineToolResult {
  /** Payload text (JSON for structured results). Relayable by the model verbatim. */
  content: string;
  /** True when the call failed or was denied — the model relays, never retries blindly. */
  isError?: boolean;
}

/**
 * The tool-execution surface the ConverseLoop hands the runner. The runner invokes
 * spine tools ONLY through this; the loop implements the six tools (gate check +
 * AppRunClient dispatch) and emits the wire events around each call.
 */
export interface ToolExecutor {
  /** The mounted tool names (always the six spine tools). */
  list(): readonly string[];
  execute(tool: string, args: Record<string, unknown>): Promise<SpineToolResult>;
}

/** Per-call permission hook (mirrors the Agent SDK canUseTool contract). */
export type CanUseTool = (
  toolName: string,
  input: Record<string, unknown>,
) => Promise<
  | { behavior: "allow"; updatedInput: Record<string, unknown> }
  | { behavior: "deny"; message: string }
>;

/** Events the runner streams back to the loop. Tool call/result events are emitted
 *  by the LOOP (it owns the executor), so the runner only reports model output,
 *  usage, and the terminal state. */
export type ConverseRunnerEvent =
  | { type: "text_delta"; text: string }
  | { type: "usage"; usage: ConverseTurnUsage }
  | {
      type: "done";
      stopReason: ConverseStopReason;
      /** SDK session id to resume the next turn with (null when the runner has none). */
      sdkSessionId: string | null;
      /** True when a missing resume target forced a fresh SDK conversation. */
      sdkSessionReset?: boolean;
      /** Present when stopReason is error/llm_* — relayed on the wire error event. */
      error?: { error_code: string; message: string; retryable: boolean };
    };

export interface ConverseRunInput {
  systemPrompt: string;
  /** The fully built turn prompt (context packet + user text, or the confirm line). */
  userMessage: string;
  /** Resume the SDK conversation from a prior turn (undefined = fresh session). */
  resumeSdkSessionId?: string;
  /** Fresh-session prompt with bounded visible history, used only if resume is missing. */
  resumeFallbackUserMessage?: string;
  /** Model id (e.g. LEAF_SPINE_MODEL). undefined => runner/account default. */
  model?: string;
  tools: ToolExecutor;
  canUseTool: CanUseTool;
}

/**
 * The conversational session runner boundary (sibling of AgentRunner). The real
 * impl is the ONLY Anthropic egress on the converse path; the fake is scripted.
 * One run() = one TURN.
 */
export interface SpineConverseRunner {
  run(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent>;
}

// --------------------------------------------------------------------------- //
// AppRunClient — the harness -> app back-edge (X-Tenant-Id + X-Dispatch-Secret).
// submitRun is THE dispatch boundary: an opaque section-7 job submission whose
// payload is {tool, params, dwg} — never code, never a drawing delta.
// --------------------------------------------------------------------------- //

/** One catalog entry as served by GET /api/capabilities (section 9 projection). */
export interface CapabilityEntry {
  name: string;
  description: string;
  capabilities: Capability[];
  params_schema?: JsonSchema;
  catalog_digest?: string;
  [k: string]: unknown;
}

export interface SubmitRunRequest {
  tenantId: string;
  /** Registered tool NAME (catalog key) — never code. */
  tool: string;
  params: Record<string, unknown>;
  dwg: string;
  catalogDigest: string;
  /** Fast-tool read path: hold the request open (?wait=1) up to waitTimeoutS. */
  wait?: boolean;
  waitTimeoutS?: number;
}

/** Section-7 job row projection ({job_id, status} + result envelope when complete). */
export interface SubmitRunResponse {
  job_id: string;
  status: string;
  result?: ResultEnvelope;
  [k: string]: unknown;
}

export interface AppRunClient {
  /** GET /api/capabilities — the tenant's catalog (data the agent searches). */
  getCapabilities(tenantId: string): Promise<CapabilityEntry[]>;
  /** GET /api/drawings/* — summary | versions | checkout fragment. */
  getDrawingState(
    tenantId: string,
    drawingId: string,
    what: "summary" | "versions" | "checkout",
  ): Promise<Record<string, unknown>>;
  /** POST /api/author after the author_tool gate grants an approved request. */
  authorTool(tenantId: string, description: string): Promise<Record<string, unknown>>;
  /** POST /api/run for registered deterministic tool execution. */
  submitRun(req: SubmitRunRequest): Promise<SubmitRunResponse>;
  /** GET /api/jobs/{id} — section-7 job row. */
  getJob(tenantId: string, jobId: string): Promise<Record<string, unknown>>;
}

// --------------------------------------------------------------------------- //
// GateClient — POST /internal/agent/gate before EVERY tool execution.
// --------------------------------------------------------------------------- //

export type GateDecision = "allow" | "deny" | "awaiting_approval";

export interface GateCheckContext {
  tenantId: string;
  sessionId: string;
  turnId: string;
  /** App-owned session/turn ids that authenticated this harness turn.
   * The loop keeps separate durable ids for resume and confirmation binding. */
  authoritySessionId?: string;
  authorityTurnId?: string;
}

export interface GateCheckResult {
  decision: GateDecision;
  /** Present when decision is awaiting_approval (app created the pending record). */
  confirmation_id?: string;
  reason?: string;
  /** Policy tier that decided (auto | confirm_once | always_confirm ...). */
  policy?: string;
  /** Blast-radius rung (R0..R7). */
  rung?: string;
}

export interface GateClient {
  check(
    action: string,
    args: Record<string, unknown>,
    ctx: GateCheckContext,
  ): Promise<GateCheckResult>;
}

// --------------------------------------------------------------------------- //
// SessionStore — durable transcript (wire contract section 6 conceptual schema).
// --------------------------------------------------------------------------- //

export type SessionStatus = "idle" | "active" | "dormant" | "archived";

export interface SessionRecord {
  session_id: string;
  tenant_id: string;
  drawing_id: string;
  sdk_session_id: string | null;
  status: SessionStatus;
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface TurnRecord {
  turn_id: string;
  session_id: string;
  seq_start: number;
  status: "active" | "complete" | "failed";
  stop_reason: string | null;
  started_at: string;
  ended_at: string | null;
}

/** One persisted wire event row (seq is per-session monotonic). */
export interface StoredEvent {
  session_id: string;
  seq: number;
  turn_id: string;
  type: ConverseEventType;
  data: Record<string, unknown>;
  ts: string;
}

export type ConfirmationStatus = "pending" | "approved" | "denied" | "expired";

export interface ConfirmationRecord {
  confirmation_id: string;
  session_id: string;
  turn_id: string;
  /** The gated action (spine tool name, e.g. run_capability). */
  action: string;
  /** Args-exact approval binding: the EXACT args the approval covers, as JSON. */
  args_json: string;
  kind: string;
  status: ConfirmationStatus;
  created_at: string;
  expires_at: string;
  decided_at: string | null;
  decided_by: string | null;
}

export interface UsageRecord {
  session_id: string;
  turn_id: string;
  usage: ConverseTurnUsage;
  ts: string;
}

/**
 * Durable session/transcript store. Implements the conceptual schema of wire
 * contract section 6 (sessions / turns / events / confirmations / usage); any
 * backend honoring these semantics (SQLite, file-backed JSONL) can swap in.
 */
export interface SessionStore {
  /** Idempotent per (tenant, drawing): returns the existing session or creates one. */
  createOrGetSession(tenantId: string, drawingId: string): Promise<SessionRecord>;
  getSession(sessionId: string): Promise<SessionRecord | null>;
  updateSession(
    sessionId: string,
    patch: Partial<Pick<SessionRecord, "sdk_session_id" | "status" | "summary">>,
  ): Promise<SessionRecord>;

  /** The turn lock: the session's turns row with status 'active', if any. */
  getActiveTurn(sessionId: string): Promise<TurnRecord | null>;
  /** Begin a turn; rejects when an active turn already exists (lock is store-enforced). */
  beginTurn(sessionId: string, turnId: string): Promise<TurnRecord>;
  endTurn(
    sessionId: string,
    turnId: string,
    status: "complete" | "failed",
    stopReason: string,
  ): Promise<void>;

  /** Append one event; assigns and persists the next per-session monotonic seq. */
  appendEvent(
    sessionId: string,
    turnId: string,
    type: ConverseEventType,
    data: Record<string, unknown>,
  ): Promise<StoredEvent>;
  /** Replay: events with seq > afterSeq, ascending (limit = most recent N of those). */
  eventsAfter(sessionId: string, afterSeq: number, limit?: number): Promise<StoredEvent[]>;

  putConfirmation(rec: ConfirmationRecord): Promise<void>;
  getConfirmation(confirmationId: string): Promise<ConfirmationRecord | null>;
  /** Resolve a pending confirmation; expired-on-arrival is marked + returned as such. */
  resolveConfirmation(
    confirmationId: string,
    approved: boolean,
    decidedBy: string,
  ): Promise<ConfirmationRecord | null>;

  appendUsage(sessionId: string, turnId: string, usage: ConverseTurnUsage): Promise<void>;
}

// --------------------------------------------------------------------------- //
// Aggregate: everything the harness server needs injected.
// --------------------------------------------------------------------------- //

export interface HarnessPorts {
  oauth: OAuthGrantProvider;
  tenantRepo: TenantRepoProvider;
  broker: BrokerApsClient;
  agentRunner: AgentRunner;
  /** Required by the live isolated customization stage/publish lifecycle. */
  customizationCoordination?: CustomizationCoordination;
  /**
   * OPTIONAL per-tenant grant admin store (wave 4). When present, the harness serves
   * PUT/GET/DELETE /grants/{tenantId}; when absent those routes return 501. The
   * hermetic author tests wire the four required ports and omit this.
   */
  grantAdmin?: TenantGrantAdminStore;
  /**
   * OPTIONAL converse-turn runner (sessions wire, leaf-backend-gaps.md §2.1).
   * When present, the harness serves `POST /turn` (NDJSON) for the FastAPI
   * turn engine to drive; when absent that route returns 501, matching the
   * `grantAdmin` precedent. Hermetic tests that don't exercise the sessions
   * lane omit this.
   */
  converseRunner?: ConverseRunner;
  /**
   * OPTIONAL §18 spine ports (PR #5) — PARKED: retained so spine modules and
   * their tests keep compiling/running, but no server surface consumes them
   * until spine unification (the /converse/* routes were unwired at the
   * 2026-07-21 merge resolution).
   */
  appRun?: AppRunClient;
  gate?: GateClient;
  sessionStore?: SessionStore;
}
