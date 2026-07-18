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

/** Author response = CONTRACT.md section 4: POST /api/author -> {tool, code, preview}. */
export interface AuthorResponse {
  tool: ToolPackage;
  code: string;
  preview: string;
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

export interface OAuthGrantProvider {
  /** Resolve the per-tenant Agent SDK grant. Concern 2 only. */
  getGrant(tenantId: string): Promise<AgentGrant>;
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
}
