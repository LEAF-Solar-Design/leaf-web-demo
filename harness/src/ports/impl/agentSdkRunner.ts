/**
 * REAL AgentRunner - the ONLY Anthropic egress in the whole harness, behind the
 * AgentRunner port. This is the LIVE Agent SDK author loop: a real Claude session
 * (authorized by ONE tenant's own OAuth grant) authors a deterministic CAD tool by
 * driving exactly three in-process MCP tools (read-only fs_tenant_repo / validate_tool /
 * aps_test_run) and nothing else, then the tool runs later with ZERO LLM.
 *
 * Shape shipped (documented in the receipt): FULL in-process tool-loop.
 *   - The model PROPOSES the tool's Python `run(intake, params)` source itself
 *     through `validate_tool` (genuine authoring, never a template).
 *   - `validate_tool` validates the source and metadata, then the trusted harness
 *     atomically writes only tool.py and tool.json. The model has no write tool.
 *   - `aps_test_run` executes the just-authored tool against the REAL intake through
 *     the REAL broker (mock path, aps_live=false) so the model can sanity-check the
 *     numbers while authoring.
 *
 * Env discipline (mirrors C:/tmp/hosted-oauth-spike): a SCRUBBED child env with the
 * grant injected EXPLICITLY via the SDK's `env` option - never inherited ambient,
 * never logged, never mingled with the Auth0 platform JWT. The grant value is only
 * ever read from AgentGrant and passed to the SDK; this file never prints it.
 *
 * Design-time only: constructed ONLY on the author/one-off/build path and torn down
 * after. The run path never reaches this file (enforced by test/designTimeOnly).
 *
 * The Agent SDK and zod are loaded through NON-LITERAL dynamic import specifiers so
 * `tsc --noEmit` over src/ compiles this file WITHOUT their (heavy) type graphs; the
 * live path requires `npm i @anthropic-ai/claude-agent-sdk` (which also brings zod).
 */

import type {
  AgentGrant,
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  AuthorTelemetry,
  ResultEnvelope,
  ToolExecutionReceipt,
  ToolPackage,
  ToolSubmissionResult,
} from "../index.js";
import { acceptBrokerTestResult } from "../../agent/tools/toolExecutionReceipt.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";
import { scrubSecrets } from "./envScrub.js";

// --------------------------------------------------------------------------- //
// Minimal local views of the SDK / zod surfaces we rely on (documented, not
// exhaustive). Kept local so the gate never typechecks against their .d.ts.
// --------------------------------------------------------------------------- //
type CallToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };
interface SdkModule {
  query(args: { prompt: string; options: Record<string, unknown> }): AsyncIterable<unknown>;
  createSdkMcpServer(opts: Record<string, unknown>): unknown;
  tool(
    name: string,
    description: string,
    inputSchema: Record<string, unknown>,
    handler: (args: Record<string, unknown>, extra: unknown) => Promise<CallToolResult>,
  ): unknown;
}
interface ZodModule {
  z: {
    string(): unknown;
    enum(v: string[]): unknown;
    array(inner: unknown): unknown;
    [k: string]: unknown;
  };
}

/** Non-literal dynamic import so tsc treats the module as `any` (compiles w/o it). */
function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

// --------------------------------------------------------------------------- //
// Secret discipline
// --------------------------------------------------------------------------- //
/** Build a scrubbed env with EXACTLY this tenant's grant injected (nothing else).
 *  Scrubbing = envScrub.ts's two-layer discipline (known ambient identities +
 *  wholesale secret-like key-name sweep, sol-critic F3/R3); the ONE selected
 *  credential variable is injected AFTER the sweep. */
export function buildScrubbedEnv(grant: AgentGrant, base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env = scrubSecrets(base);
  if (grant.kind === "oauth") {
    env.CLAUDE_CODE_OAUTH_TOKEN = grant.oauthToken;
  } else {
    env.ANTHROPIC_API_KEY = grant.apiKey;
  }
  return env;
}

// --------------------------------------------------------------------------- //
// Usage self-metering (research/agentsdk-usage-visibility.md: there is NO balance
// API - meter from each response's usage). Exposed on the runner instance so a
// driver can read it out of band (the frozen {tool,code,preview} response can't
// carry it).
// --------------------------------------------------------------------------- //
export interface TurnUsage {
  turn: number;
  model?: string;
  input_tokens: number;
  output_tokens: number;
  cache_creation_input_tokens: number;
  cache_read_input_tokens: number;
}
export interface RunUsageSummary {
  turns: number;
  usage_totals: {
    input_tokens: number;
    output_tokens: number;
    cache_creation_input_tokens: number;
    cache_read_input_tokens: number;
    total_tokens: number;
  };
  total_cost_usd: number | null;
  models: string[];
  session_id: string | null;
  per_turn: TurnUsage[];
  result_subtype: string | null;
}

export interface AgentSdkRunnerOptions {
  /** undefined => the SDK/account default model. */
  model?: string;
  /** Spend cap: abort the author loop past this many SDK turns (contract: <= 40). */
  maxTurns?: number;
  /** Spend cap: abort past this many total tokens (contract: ~500k). */
  maxTotalTokens?: number;
}

export interface AuthorBrokerTestState {
  attempted: boolean;
  ok: boolean;
  receipt: ToolExecutionReceipt | null;
  failureReason: string | null;
  errorCode: string | null;
}

function emptyBrokerTestState(): AuthorBrokerTestState {
  return {
    attempted: false,
    ok: false,
    receipt: null,
    failureReason: null,
    errorCode: null,
  };
}

function brokerTestState(envelope: ResultEnvelope): AuthorBrokerTestState {
  const accepted = acceptBrokerTestResult(envelope);
  const rawCode = envelope.error?.error_code;
  return {
    attempted: true,
    ok: accepted.ok,
    receipt: accepted.receipt,
    failureReason: accepted.ok ? null : (accepted.reason ?? "broker test was not accepted"),
    errorCode: typeof rawCode === "string" && /^[A-Z0-9_]{1,100}$/.test(rawCode)
      ? rawCode
      : null,
  };
}

const AUTHOR_TOOL_NAMES = [
  "mcp__author__fs_tenant_repo",
  "mcp__author__validate_tool",
  "mcp__author__aps_test_run",
];

// --------------------------------------------------------------------------- //
// Optional read-only registry attachment
// --------------------------------------------------------------------------- //
/**
 * Attach the tenant's tool-registry MCP face (leaf-tool-registry,
 * https://studio.leafdesign.ai/api/mcp) to the author session -- READ-ONLY
 * consultation so the model version-bumps an existing tool instead of
 * authoring a duplicate. OFF unless all three env vars are set on the harness
 * process:
 *
 *   LEAF_REGISTRY_MCP_URL     the face's URL
 *   LEAF_REGISTRY_MCP_TOKEN   the face's edge bearer (CW_STUDIO_PUBLIC_TOKEN)
 *   LEAF_REGISTRY_MCP_TENANT  the studio deployment UUID for this tenant
 *
 * Secret discipline mirrors the grant: the bearer rides ONLY the per-server
 * headers option (never childEnv, never logged). The face exposes only query
 * tools (registry_list / registry_get), so the mutating surface of the author
 * session is unchanged: still exactly the three author tools. Per-tenant
 * mapping (platform tenant -> studio deployment) is a follow-up; this v1
 * serves the single-tenant/demo wiring.
 */
export type RegistryMcpAttachment = {
  serverConfig: { type: "http"; url: string; headers: Record<string, string> };
  toolNames: string[];
};

export const REGISTRY_TOOL_NAMES = [
  "mcp__registry__registry_list",
  "mcp__registry__registry_get",
];

export function registryMcpAttachment(env: NodeJS.ProcessEnv = process.env): RegistryMcpAttachment | null {
  const url = env.LEAF_REGISTRY_MCP_URL?.trim();
  const token = env.LEAF_REGISTRY_MCP_TOKEN?.trim();
  const tenant = env.LEAF_REGISTRY_MCP_TENANT?.trim();
  if (!url || !token || !tenant) return null;
  return {
    serverConfig: {
      type: "http",
      url,
      headers: { Authorization: `Bearer ${token}`, Cookie: `tenant_id=${tenant}` },
    },
    toolNames: [...REGISTRY_TOOL_NAMES],
  };
}

/** Consultation guide appended to the prompt only when the registry is attached. */
const REGISTRY_GUIDE = `
=== Registry (read-only) ===
Before authoring, call registry_list to see this tenant's already-registered
tools. If an equivalent tool already exists, version-bump/extend it instead of
authoring a duplicate; use registry_get(pack) for its recorded versions.
`;
export const AUTHOR_FS_ACTIONS = ["read", "list", "exists"] as const;

/**
 * Runner-specific authoring guide appended to the shared system prompt. Names the
 * three real tools, the tool.py contract, and the platform Intake JSON shape so the
 * model writes correct field access. It describes the DATA, never the algorithm -
 * the model authors the logic.
 */
export const AUTHOR_RUNNER_GUIDE = `
=== How to author (drive these three tools, nothing else) ===
1. Choose a kebab-case tool NAME and a snake_case ENGINE_OP.
2. Write the entry-script source in your validate_tool call. Do not write repo files.
   Contract of the submitted source (runs later with ZERO LLM):
     def run(intake, params):
         # pure standard-library Python; deterministic; no I/O, no network.
         # return (result, overlay)  where result is a JSON object (dict) and
         # overlay is None (or a small dict of highlights).
         return (result_dict, None)
3. Call validate_tool with source plus your metadata (name, description, engine_op,
   params_schema_json, returns_schema_json, capabilities). The trusted harness
   validates and writes exactly tools/<name>/tool.py and tool.json. Fix any
   diagnostics and resubmit until it says VALID.
4. Call aps_test_run (optionally with params_json) to run your tool on the REAL
   drawing intake through the broker and inspect the computed result. Confirm the
   numbers look sane; fix tool.py + re-validate if not.
5. Stop once validate_tool says VALID and aps_test_run returned ok:true with a
   sensible result. Do not write anything outside tools/<name>/.

Be efficient: you do NOT need to read the repo or other tools first. Submit once
(fix and resubmit only if it reports diagnostics), call aps_test_run
once to confirm the numbers, then stop. Avoid unnecessary exploration.

=== Platform Intake JSON shape (what your tool.py receives as "intake") ===
A dict extracted from the drawing:
  intake["layers"]    -> list[str] of layer names, e.g. ["0","Panels","Panel Groups","Defpoints"].
  intake["polylines"] -> list of {"layer": str, "closed": bool,
                          "pts": [[x, y, z], ...],   # vertices; use pt[0]=x, pt[1]=y
                          "handle": str}. Coordinates are floats (can be large, e.g. ~14000..21000).
  intake["inserts"]   -> list of {"layer": str, ...} (block references; may be empty).
  intake["faces3d"]   -> list of {"layer": str, ...} (3D faces; may be empty).
"params" is a dict of optional caller inputs matching your params JSON Schema.

=== Capability and drawing-write contract ===
Choose capabilities from the request:
- Read-only analysis: ["drawing.read"].
- A tool that changes panel positions or drawing geometry: ["drawing.write"].

For drawing.write, tool.py stays pure. It does not write files or versions. Return
the proposed edit in result["mutations"].

To add geometry, use result["mutations"]["added"] with one or more intake-shaped
closed polylines. Each added entity must contain a deterministic unique handle,
a layer, closed:true, and at least three [x, y, z] points. Represent requested
prisms and cylinders as their closed 2D drawing footprints. Compute placement
from the existing intake bounds. For example:
  {"added": [
    {"handle": "LEAF-AUTHORED-1", "layer": "LEAF_AUTHORED",
     "closed": true, "pts": [[0,0,0], [10,0,0], [10,10,0], [0,10,0]]}
  ]}

To move existing panels, use:
  {"transforms": [
    {"handle": "AB12", "dx": 10.0, "dy": -5.0, "rotation_deg": 0.0}
  ]}
Each handle must name one existing intake polyline. dx and dy are offsets from the
panel's current centroid, not absolute coordinates. Preserve panels by emitting
exactly one transform for every selected panel and no added or removed entities.
Keep abs(dx) and abs(dy) <= 10000 and rotation_deg in [-360, 360].

Every drawing.write params schema must include:
  "drawing_id": string, default "cat-workbench"
  "dry_run": boolean, default false
When dry_run is true, compute and return the same proposed mutations and preview;
the platform will not apply them or create a version. aps_test_run automatically
forces dry_run=true for drawing.write candidates, so it is safe. A normal approved
runtime call applies the mutations and the platform creates immutable vN+1.

For a panel silhouette, derive target centroids deterministically, sort source
panels by stable handle, pair them in that order, and return one transform per
panel. Do not invent a special engine_op implementation. The persisted tool.py is
the implementation and may use any descriptive snake_case engine_op.`;

// --------------------------------------------------------------------------- //
// The runner
// --------------------------------------------------------------------------- //
/**
 * Rate-limit state of the most recent run() (B3 hardening, ADDITIVE). Exposed so a
 * driver can read the retry-after horizon out of band; `retry_after_s` is derived
 * from the SDK's `rate_limit_event` resetsAt (subscription lane) or the last
 * `api_retry` delay, and is null when the SDK reported no horizon.
 */
export interface RateLimitState {
  /** The SDK assistant error that tripped the abort (always "rate_limit"). */
  error: string;
  /** Seconds until the limit resets, when the SDK reported one. */
  retry_after_s: number | null;
}

export class AgentSdkRunner implements AgentRunner {
  /** Per-turn usage from the most recent run() (self-metered). */
  usageLog: TurnUsage[] = [];
  /** Summary of the most recent run() (read out of band by a driver). */
  lastRun: RunUsageSummary | null = null;
  /** Rate-limit info from the most recent run(), or null (B3, additive). */
  lastRateLimit: RateLimitState | null = null;

  constructor(private readonly opts: AgentSdkRunnerOptions = {}) {}

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    const maxTurns = this.opts.maxTurns ?? 24;
    const maxTotalTokens = this.opts.maxTotalTokens ?? 500_000;
    this.usageLog = [];
    this.lastRun = null;
    this.lastRateLimit = null;

    // 1) Scrub env + inject THIS tenant's grant explicitly (never logged).
    const childEnv = buildScrubbedEnv(input.grant, process.env);

    // 2) Load the SDK + zod (operator-gated; dynamic so the stub compiles w/o them).
    const sdk = (await dynImport(["@anthropic-ai", "claude-agent-sdk"])) as unknown as SdkModule;
    const { z } = (await dynImport(["zod"])) as unknown as ZodModule;

    // 3) The three author tools, bound to the harness toolset. The model can inspect
    //    its tenant repo but cannot write a path. Source and metadata cross one
    //    structured submit boundary owned by the harness.
    let candidate: ToolPackage | null = null;
    let candidateSubmission: ReturnType<AgentRunInput["toolset"]["submitTool"]> | null = null;
    let candidateTest = emptyBrokerTestState();
    const fs = input.toolset.fsTenantRepo;
    const ok = (text: string): CallToolResult => ({ content: [{ type: "text", text }] });
    const bad = (text: string): CallToolResult => ({ content: [{ type: "text", text }], isError: true });

    const fsTool = sdk.tool(
      "fs_tenant_repo",
      "Read-only inspection scoped to the tenant repo. action is read, list, or exists. Paths are repo-relative; escapes are rejected. This tool cannot write.",
      {
        action: z.enum([...AUTHOR_FS_ACTIONS]),
        path: z.string(),
      },
      async (args): Promise<CallToolResult> => {
        const action = String(args.action);
        const path = String(args.path ?? "");
        try {
          if (action === "read") return ok(fs.readFile(path));
          if (action === "list") return ok(JSON.stringify(fs.listDir(path || ".")));
          if (action === "exists") return ok(String(fs.exists(path)));
          return bad(`unknown action ${action}`);
        } catch (e) {
          return bad(`fs error: ${(e as Error).message}`);
        }
      },
    );

    const validateTool = sdk.tool(
      "validate_tool",
      "Submit Python source and manifest metadata. The trusted harness validates them and atomically writes only tools/<name>/tool.py and tool.json. Returns VALID plus exact-byte hashes or diagnostics.",
      {
        name: z.string(),
        description: z.string(),
        engine_op: z.string(),
        source: z.string(),
        params_schema_json: z.string(),
        returns_schema_json: z.string(),
        capabilities: z.array(z.string()),
      },
      async (a): Promise<CallToolResult> => {
        try {
          const name = String(a.name);
          const params = JSON.parse(String(a.params_schema_json));
          const returns = JSON.parse(String(a.returns_schema_json));
          const caps = Array.isArray(a.capabilities) ? (a.capabilities as unknown[]).map(String) : [];
          const submitted = input.toolset.submitTool({
            name,
            description: String(a.description),
            engine_op: String(a.engine_op),
            params,
            returns,
            capabilities: caps as ToolPackage["capabilities"],
            source: String(a.source),
            session: "agent-sdk",
          });
          candidate = submitted.tool;
          candidateSubmission = submitted;
          candidateTest = emptyBrokerTestState();
          return ok(JSON.stringify({
            status: "VALID",
            tool: submitted.tool,
            source_receipt: submitted.receipt,
            next: "Call aps_test_run. The author result is refused until that broker test passes.",
          }));
        } catch (e) {
          return bad(`validate error: ${(e as Error).message}`);
        }
      },
    );

    const apsTestRun = sdk.tool(
      "aps_test_run",
      "Run the current validated candidate tool on the real drawing intake through the broker (mock path, aps_live=false) and return the section-3 result envelope. Call validate_tool first.",
      { params_json: (z.string() as { optional(): unknown }).optional() },
      async (a): Promise<CallToolResult> => {
        try {
          if (!candidate) return bad("no validated candidate yet - call validate_tool first.");
          const params = typeof a.params_json === "string" && a.params_json.trim()
            ? JSON.parse(a.params_json)
            : {};
          candidateTest = { ...candidateTest, attempted: true };
          const env = await input.toolset.apsTestRun(candidate, params);
          candidateTest = brokerTestState(env);
          const accepted = acceptBrokerTestResult(env);
          const text = JSON.stringify(env, null, 2);
          return accepted.ok
            ? ok(text)
            : bad(JSON.stringify({ ...env, receipt_error: accepted.reason }, null, 2));
        } catch (e) {
          return bad(`test-run error: ${(e as Error).message}`);
        }
      },
    );

    const server = sdk.createSdkMcpServer({
      name: "author",
      version: "1.0.0",
      tools: [fsTool, validateTool, apsTestRun],
    });

    // 4) One design-time session restricted to EXACTLY the three author tools,
    //    plus (when configured) the READ-ONLY registry consultation pair.
    const abort = new AbortController();
    const registry = registryMcpAttachment();
    const allowedNames = registry ? [...AUTHOR_TOOL_NAMES, ...registry.toolNames] : AUTHOR_TOOL_NAMES;
    const allowed = new Set(allowedNames);
    const q = sdk.query({
      prompt: `${input.systemPrompt}\n${AUTHOR_RUNNER_GUIDE}${registry ? REGISTRY_GUIDE : ""}\n\nAuthor a tool for this request:\n${input.description}`,
      options: {
        env: childEnv,
        model: this.opts.model,
        maxTurns,
        settingSources: [],
        permissionMode: "default",
        cwd: input.repoDir,
        abortController: abort,
        mcpServers: registry ? { author: server, registry: registry.serverConfig } : { author: server },
        allowedTools: allowedNames,
        canUseTool: async (toolName: string, inp: Record<string, unknown>) => {
          if (allowed.has(toolName)) return { behavior: "allow", updatedInput: inp };
          return {
            behavior: "deny",
            message: `tool ${toolName} is not permitted in the design-time author session (only fs_tenant_repo, validate_tool, aps_test_run${registry ? ", and read-only registry_list/registry_get" : ""}).`,
          };
        },
      },
    });

    // 5) Drain the session; self-meter per turn; enforce the spend cap.
    //    Cost-relevant tokens = input + output + cache_creation. cache_read is the
    //    (cheap) replayed conversation context and is EXCLUDED from the cap so a
    //    normal multi-turn cached session does not trip a false "500k" ceiling; it
    //    is still recorded per turn for full transparency.
    let turn = 0;
    let cumulative = 0;
    let result: Record<string, unknown> | null = null;
    let authFailure: string | null = null;
    let capHit: string | null = null;
    // B3 (ADDITIVE): rate-limit horizon sources. resetsAt arrives on
    // rate_limit_event (subscription lane, epoch seconds or ms); api_retry
    // carries the SDK's own backoff delay. Neither alters the existing flow.
    let rateLimitHit = false;
    let rateResetsAtS: number | null = null;
    let retryDelayMs: number | null = null;
    for await (const raw of q) {
      const msg = raw as Record<string, unknown>;
      if (msg.type === "rate_limit_event") {
        const info = (msg.rate_limit_info ?? {}) as Record<string, unknown>;
        if (typeof info.resetsAt === "number") {
          rateResetsAtS = info.resetsAt > 1e12 ? info.resetsAt / 1000 : info.resetsAt;
        }
      } else if (msg.type === "system" && msg.subtype === "api_retry") {
        if (typeof msg.retry_delay_ms === "number") retryDelayMs = msg.retry_delay_ms;
      }
      if (msg.type === "assistant") {
        turn += 1;
        const message = (msg.message ?? {}) as Record<string, unknown>;
        const u = (message.usage ?? {}) as Record<string, number>;
        const rec: TurnUsage = {
          turn,
          model: typeof message.model === "string" ? message.model : undefined,
          input_tokens: u.input_tokens ?? 0,
          output_tokens: u.output_tokens ?? 0,
          cache_creation_input_tokens: u.cache_creation_input_tokens ?? 0,
          cache_read_input_tokens: u.cache_read_input_tokens ?? 0,
        };
        this.usageLog.push(rec);
        cumulative += rec.input_tokens + rec.output_tokens + rec.cache_creation_input_tokens;
        const err = msg.error;
        if (typeof err === "string" && ["authentication_failed", "oauth_org_not_allowed", "billing_error"].includes(err)) {
          authFailure = err;
          abort.abort();
          break;
        }
        // B3 (ADDITIVE): "rate_limit" joined SDKAssistantMessageError in SDK
        // 0.3.214 — treat it as terminal too (previously it fell through as a
        // generic session failure). Existing kinds above are untouched.
        if (err === "rate_limit") {
          rateLimitHit = true;
          abort.abort();
          break;
        }
        if (turn > maxTurns || cumulative > maxTotalTokens) {
          capHit = `Agent SDK spend cap exceeded (turns=${turn} > ${maxTurns} or cost-tokens=${cumulative} > ${maxTotalTokens})`;
          abort.abort();
          break;
        }
      } else if (msg.type === "result") {
        result = msg;
      }
    }

    // 6) Summarize usage (authoritative totals from the result message when present).
    const rUsage = (result?.usage ?? {}) as Record<string, number>;
    const summed = this.usageLog.reduce(
      (acc, r) => {
        acc.input_tokens += r.input_tokens;
        acc.output_tokens += r.output_tokens;
        acc.cache_creation_input_tokens += r.cache_creation_input_tokens;
        acc.cache_read_input_tokens += r.cache_read_input_tokens;
        return acc;
      },
      { input_tokens: 0, output_tokens: 0, cache_creation_input_tokens: 0, cache_read_input_tokens: 0 },
    );
    const totals = {
      input_tokens: rUsage.input_tokens ?? summed.input_tokens,
      output_tokens: rUsage.output_tokens ?? summed.output_tokens,
      cache_creation_input_tokens: rUsage.cache_creation_input_tokens ?? summed.cache_creation_input_tokens,
      cache_read_input_tokens: rUsage.cache_read_input_tokens ?? summed.cache_read_input_tokens,
    };
    const modelUsage = (result?.modelUsage ?? {}) as Record<string, unknown>;
    const models = Object.keys(modelUsage).length
      ? Object.keys(modelUsage)
      : [...new Set(this.usageLog.map((r) => r.model).filter((m): m is string => !!m))];
    this.lastRun = {
      turns: typeof result?.num_turns === "number" ? (result.num_turns as number) : turn,
      usage_totals: {
        ...totals,
        total_tokens:
          totals.input_tokens + totals.output_tokens + totals.cache_creation_input_tokens + totals.cache_read_input_tokens,
      },
      total_cost_usd: typeof result?.total_cost_usd === "number" ? (result.total_cost_usd as number) : null,
      models,
      session_id: typeof result?.session_id === "string" ? (result.session_id as string) : null,
      per_turn: this.usageLog,
      result_subtype: typeof result?.subtype === "string" ? (result.subtype as string) : null,
    };

    // 6b) Surface a terminal auth / spend-cap failure (usage is now captured above).
    if (authFailure) throw new Error(`Agent SDK auth failure: ${authFailure}`);
    if (capHit) throw new Error(capHit);
    // B3 (ADDITIVE): rate-limit terminal, with the retry-after horizon exposed on
    // the instance (lastRateLimit) and named in the thrown error.
    if (rateLimitHit) {
      const retryAfterS =
        rateResetsAtS !== null
          ? Math.max(0, Math.round(rateResetsAtS - Date.now() / 1000))
          : retryDelayMs !== null
            ? Math.round(retryDelayMs / 1000)
            : null;
      this.lastRateLimit = { error: "rate_limit", retry_after_s: retryAfterS };
      throw new Error(
        `Agent SDK rate limited` +
          (retryAfterS !== null ? ` (retry after ~${retryAfterS}s)` : " (retry horizon unknown)"),
      );
    }

    // 7) The candidate must be a validated package (defense in depth: re-validate).
    const finalSubmission = candidateSubmission as ToolSubmissionResult | null;
    if (!candidate || !finalSubmission) {
      throw new Error(
        `author session ended without a validated tool (result subtype=${this.lastRun.result_subtype ?? "n/a"}, turns=${turn}).`,
      );
    }
    const candidateExecutionReceipt = await completeRequiredBrokerTest(
      candidate,
      input.toolset.apsTestRun,
      candidateTest,
    );
    const finalPkg: ToolPackage = candidate;
    const diagnostics = validateToolPackage(finalPkg);
    if (diagnostics.length > 0) {
      throw new Error(`authored tool failed re-validation: ${diagnostics.join("; ")}`);
    }
    return {
      tool: finalPkg,
      code: finalSubmission.code,
      preview: `Tool "${finalPkg.name}" authored via the Agent SDK (engine_op=${finalPkg.engine_op}); runs zero-LLM at runtime.`,
      files: finalSubmission.files,
      sourceReceipt: finalSubmission.receipt,
      ...(candidateExecutionReceipt
        ? { executionReceipt: candidateExecutionReceipt }
        : {}),
      // A1: surface the REAL self-metered authoring telemetry (turns/tokens/cost/models)
      // from the run just completed, so /author can carry a provenance/telemetry chip.
      // `lastRun` is unconditionally assigned in step 6 above before we reach here.
      telemetry: telemetryFromSummary(this.lastRun!),
    };
  }
}

/**
 * Enforce the broker test as a trusted harness gate even when the model stops
 * after source validation. The model-facing tool remains useful for iterative
 * feedback, but correctness no longer depends on the model choosing to call it.
 */
export async function completeRequiredBrokerTest(
  candidate: ToolPackage,
  apsTestRun: AgentRunInput["toolset"]["apsTestRun"],
  state: AuthorBrokerTestState = emptyBrokerTestState(),
): Promise<ToolExecutionReceipt | null> {
  const finalState = state.attempted
    ? state
    : brokerTestState(await apsTestRun(candidate, {}));
  if (!finalState.ok) {
    throw new Error(
      `authored tool failed required broker test${finalState.errorCode ? ` (${finalState.errorCode})` : ""}: ${finalState.failureReason ?? "broker test was not accepted"}.`,
    );
  }
  return finalState.receipt;
}

/**
 * Project the runner's self-metered {@link RunUsageSummary} into the additive
 * {@link AuthorTelemetry} surfaced on the /author response. Absent-safe: a dimension
 * the SDK did not report is OMITTED (never a fabricated 0/null) — `total_cost_usd` is
 * dropped when the SDK gave no cost, and `models` is dropped when empty.
 */
function telemetryFromSummary(summary: RunUsageSummary): AuthorTelemetry {
  const t: AuthorTelemetry = {
    turns: summary.turns,
    input_tokens: summary.usage_totals.input_tokens,
    output_tokens: summary.usage_totals.output_tokens,
  };
  if (typeof summary.total_cost_usd === "number") t.total_cost_usd = summary.total_cost_usd;
  if (summary.models.length > 0) t.models = summary.models;
  return t;
}
