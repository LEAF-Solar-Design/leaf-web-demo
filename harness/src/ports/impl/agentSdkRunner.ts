/**
 * REAL AgentRunner - the ONLY Anthropic egress in the whole harness, behind the
 * AgentRunner port. This is the LIVE Agent SDK author loop: a real Claude session
 * (authorized by ONE tenant's own OAuth grant) authors a deterministic CAD tool by
 * driving exactly three in-process MCP tools (fs_tenant_repo / validate_tool /
 * aps_test_run) and nothing else, then the tool runs later with ZERO LLM.
 *
 * Shape shipped (documented in the receipt): FULL in-process tool-loop.
 *   - The model WRITES the tool's Python `run(intake, params)` body itself, via the
 *     scoped `fs_tenant_repo` tool (genuine authoring - never a template).
 *   - `validate_tool` assembles the CONTRACT section 2 registry package from the
 *     model's chosen metadata (deterministic, harness-owned - so a malformed
 *     manifest can never break the pipeline) and runs the section-2 oracle.
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
  ToolPackage,
} from "../index.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";
import { readFileSync } from "node:fs";
import { join } from "node:path";

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
/** Env vars that must NOT leak an ambient Anthropic identity into the session. */
const AMBIENT_CRED_KEYS = [
  "ANTHROPIC_API_KEY",
  "ANTHROPIC_AUTH_TOKEN",
  "CLAUDE_CODE_OAUTH_TOKEN",
  "CLAUDE_CODE_USE_BEDROCK",
  "CLAUDE_CODE_USE_VERTEX",
  "CLAUDE_CODE_USE_FOUNDRY",
];

/** Build a scrubbed env with EXACTLY this tenant's grant injected (nothing else). */
export function buildScrubbedEnv(grant: AgentGrant, base: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...base };
  for (const k of AMBIENT_CRED_KEYS) delete env[k];
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

const AUTHOR_TOOL_NAMES = [
  "mcp__author__fs_tenant_repo",
  "mcp__author__validate_tool",
  "mcp__author__aps_test_run",
];

/**
 * Runner-specific authoring guide appended to the shared system prompt. Names the
 * three real tools, the tool.py contract, and the platform Intake JSON shape so the
 * model writes correct field access. It describes the DATA, never the algorithm -
 * the model authors the logic.
 */
const RUNNER_GUIDE = `
=== How to author (drive these three tools, nothing else) ===
1. Choose a kebab-case tool NAME and a snake_case ENGINE_OP.
2. Write the entry script to "tools/<name>/tool.py" using fs_tenant_repo(action:"write").
   Contract of tool.py (runs later with ZERO LLM):
     def run(intake, params):
         # pure standard-library Python; deterministic; no I/O, no network.
         # return (result, overlay)  where result is a JSON object (dict) and
         # overlay is None (or a small dict of highlights).
         return (result_dict, None)
3. Call validate_tool with your metadata (name, description, engine_op,
   params_schema_json, returns_schema_json, capabilities). It assembles + writes
   the tool.json manifest and runs the CONTRACT section 2 oracle. Fix any
   diagnostics and re-validate until it says VALID.
4. Call aps_test_run (optionally with params_json) to run your tool on the REAL
   drawing intake through the broker and inspect the computed result. Confirm the
   numbers look sane; fix tool.py + re-validate if not.
5. Stop once validate_tool says VALID and aps_test_run returned ok:true with a
   sensible result. Do not write anything outside tools/<name>/.

Be efficient: you do NOT need to read the repo or other tools first. Write tool.py,
validate once (fix and re-validate only if it reports diagnostics), aps_test_run
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
Capabilities: this is a read-only analysis tool -> capabilities: ["drawing.read"].`;

// --------------------------------------------------------------------------- //
// The runner
// --------------------------------------------------------------------------- //
export class AgentSdkRunner implements AgentRunner {
  /** Per-turn usage from the most recent run() (self-metered). */
  usageLog: TurnUsage[] = [];
  /** Summary of the most recent run() (read out of band by a driver). */
  lastRun: RunUsageSummary | null = null;

  constructor(private readonly opts: AgentSdkRunnerOptions = {}) {}

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    const maxTurns = this.opts.maxTurns ?? 24;
    const maxTotalTokens = this.opts.maxTotalTokens ?? 500_000;
    this.usageLog = [];
    this.lastRun = null;

    // 1) Scrub env + inject THIS tenant's grant explicitly (never logged).
    const childEnv = buildScrubbedEnv(input.grant, process.env);

    // 2) Load the SDK + zod (operator-gated; dynamic so the stub compiles w/o them).
    const sdk = (await dynImport(["@anthropic-ai", "claude-agent-sdk"])) as unknown as SdkModule;
    const { z } = (await dynImport(["zod"])) as unknown as ZodModule;

    // 3) The three author tools, bound to the harness toolset. The candidate is the
    //    assembled+validated package the harness will register (no disk read-back of
    //    a model-written manifest -> robust).
    let candidate: ToolPackage | null = null;
    const fs = input.toolset.fsTenantRepo;
    const ok = (text: string): CallToolResult => ({ content: [{ type: "text", text }] });
    const bad = (text: string): CallToolResult => ({ content: [{ type: "text", text }], isError: true });

    const fsTool = sdk.tool(
      "fs_tenant_repo",
      "Read/write files scoped to the tenant repo. action:'write' creates a file (e.g. tools/<name>/tool.py); 'read'/'list'/'exists' inspect. Paths are repo-relative; escapes are rejected.",
      {
        action: z.enum(["read", "write", "list", "exists"]),
        path: z.string(),
        content: (z.string() as { optional(): unknown }).optional(),
      },
      async (args): Promise<CallToolResult> => {
        const action = String(args.action);
        const path = String(args.path ?? "");
        try {
          if (action === "read") return ok(fs.readFile(path));
          if (action === "write") {
            const content = typeof args.content === "string" ? args.content : "";
            fs.writeFile(path, content);
            return ok(`wrote ${path} (${content.length} bytes)`);
          }
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
      "Assemble the CONTRACT section 2 tool package from your metadata, write tools/<name>/tool.json, and run the section-2 oracle. The entry file tools/<name>/tool.py must already exist. Returns VALID or a list of diagnostics.",
      {
        name: z.string(),
        description: z.string(),
        engine_op: z.string(),
        params_schema_json: z.string(),
        returns_schema_json: z.string(),
        capabilities: z.array(z.string()),
      },
      async (a): Promise<CallToolResult> => {
        try {
          const name = String(a.name);
          const entryRel = `tools/${name}/tool.py`;
          if (!fs.exists(entryRel)) {
            return bad(`entry file ${entryRel} does not exist yet - write it via fs_tenant_repo(write) before validating.`);
          }
          const params = JSON.parse(String(a.params_schema_json));
          const returns = JSON.parse(String(a.returns_schema_json));
          const caps = Array.isArray(a.capabilities) ? (a.capabilities as unknown[]).map(String) : [];
          const now = new Date().toISOString();
          const pkg = {
            name,
            version: "1.0.0",
            description: String(a.description),
            kind: "script",
            engine_op: String(a.engine_op),
            entry: entryRel,
            params,
            returns,
            capabilities: caps,
            timeout_ms: 30000,
            idempotent: true,
            review: { status: "unreviewed" },
            provenance: { author: "agent", created: now, modified: now, session: "agent-sdk", static_scan: [] },
          } as unknown as ToolPackage;
          const diagnostics = validateToolPackage(pkg);
          // Always persist the manifest (package-relative entry per SPEC section 7.1).
          const manifest = { ...pkg, entry: "tool.py" };
          fs.writeFile(`tools/${name}/tool.json`, JSON.stringify(manifest, null, 2) + "\n");
          if (diagnostics.length === 0) {
            candidate = pkg;
            return ok('VALID: tool package passes CONTRACT section 2. Next: aps_test_run to confirm the numbers, then stop.');
          }
          return bad("INVALID (fix and re-validate):\n- " + diagnostics.join("\n- "));
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
          const env = await input.toolset.apsTestRun(candidate, params);
          const text = JSON.stringify(env, null, 2);
          return env.ok ? ok(text) : bad(text);
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

    // 4) One design-time session restricted to EXACTLY the three tools.
    const abort = new AbortController();
    const allowed = new Set(AUTHOR_TOOL_NAMES);
    const q = sdk.query({
      prompt: `${input.systemPrompt}\n${RUNNER_GUIDE}\n\nAuthor a tool for this request:\n${input.description}`,
      options: {
        env: childEnv,
        model: this.opts.model,
        maxTurns,
        settingSources: [],
        permissionMode: "default",
        cwd: input.repoDir,
        abortController: abort,
        mcpServers: { author: server },
        allowedTools: AUTHOR_TOOL_NAMES,
        canUseTool: async (toolName: string, inp: Record<string, unknown>) => {
          if (allowed.has(toolName)) return { behavior: "allow", updatedInput: inp };
          return {
            behavior: "deny",
            message: `tool ${toolName} is not permitted in the design-time author session (only fs_tenant_repo, validate_tool, aps_test_run).`,
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
    for await (const raw of q) {
      const msg = raw as Record<string, unknown>;
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

    // 7) The candidate must be a validated package (defense in depth: re-validate).
    if (!candidate) {
      throw new Error(
        `author session ended without a validated tool (result subtype=${this.lastRun.result_subtype ?? "n/a"}, turns=${turn}).`,
      );
    }
    const finalPkg: ToolPackage = candidate;
    const diagnostics = validateToolPackage(finalPkg);
    if (diagnostics.length > 0) {
      throw new Error(`authored tool failed re-validation: ${diagnostics.join("; ")}`);
    }
    const code = finalPkg.entry ? safeRead(join(input.repoDir, finalPkg.entry)) : "";
    return {
      tool: finalPkg,
      code,
      preview: `Tool "${finalPkg.name}" authored via the Agent SDK (engine_op=${finalPkg.engine_op}); runs zero-LLM at runtime.`,
      files: finalPkg.entry ? [finalPkg.entry, `tools/${finalPkg.name}/tool.json`] : [],
    };
  }
}

function safeRead(path: string): string {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}
