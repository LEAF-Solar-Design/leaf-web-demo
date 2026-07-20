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

export interface TenantRepoProvider {
  /** Provide a checkout of the tenant's repo (git working copy + commit()). */
  checkout(tenantId: string): Promise<TenantRepo>;
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
// Aggregate: everything the harness server needs injected.
// --------------------------------------------------------------------------- //

export interface HarnessPorts {
  oauth: OAuthGrantProvider;
  tenantRepo: TenantRepoProvider;
  broker: BrokerApsClient;
  agentRunner: AgentRunner;
  /**
   * OPTIONAL per-tenant grant admin store (wave 4). When present, the harness serves
   * PUT/GET/DELETE /grants/{tenantId}; when absent those routes return 501. The
   * hermetic author tests wire the four required ports and omit this.
   */
  grantAdmin?: TenantGrantAdminStore;
}
