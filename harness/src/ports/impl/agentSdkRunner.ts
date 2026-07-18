/**
 * REAL AgentRunner - the ONLY Anthropic egress in the whole harness, behind the
 * AgentRunner port. Its live path is OPERATOR-GATED (needs a real per-tenant grant
 * + the installed @anthropic-ai/claude-agent-sdk + the app registered for "sign in
 * with Claude"); it COMPILES now so the port contract is real.
 *
 * Corrected OAuth reality (research/agentsdk-usage-visibility.md, 2026-07-17):
 *   - The June-2026 monthly *dollar credit* program is PAUSED. Agent SDK usage
 *     draws from the user's existing subscription rate windows (5h + weekly), NOT
 *     a dedicated per-user credit. There is NO balance API for a subscription app.
 *   - Subscription OAuth tokens are INDIVIDUAL-USE: exactly one per end user,
 *     NEVER shared/pooled. This runner injects ONE tenant's own grant per session.
 *   - `claude setup-token` mints a 1-year token consumed as CLAUDE_CODE_OAUTH_TOKEN.
 *
 * Env discipline mirrors the hosted-oauth-spike sibling (C:/tmp/hosted-oauth-spike):
 * a SCRUBBED child env with the grant injected EXPLICITLY (never inherited from the
 * ambient process env, never logged, never mingled with the Auth0 platform JWT).
 *
 * Design-time only: this is constructed ONLY on the author/one-off/build path and
 * torn down after. The run path never reaches this file.
 */

import type {
  AgentGrant,
  AgentRunInput,
  AgentRunResult,
  AgentRunner,
  ToolPackage,
} from "../index.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

/** Minimal structural view of the SDK surface we rely on (documented, not exhaustive). */
interface AgentSdkModule {
  query(args: {
    prompt: string;
    options: Record<string, unknown>;
  }): AsyncIterable<unknown> & { accountInfo?: () => Promise<unknown> };
}

/**
 * Load the Agent SDK WITHOUT a static import specifier, so `tsc --noEmit` compiles
 * this stub even when the (heavy, operator-installed) SDK package is absent. The
 * live path requires the operator to `npm i @anthropic-ai/claude-agent-sdk`.
 */
function sdkSpecifier(): string {
  // Non-literal so tsc does not attempt module resolution (Promise<any>).
  return ["@anthropic-ai", "claude-agent-sdk"].join("/");
}

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

export interface AgentSdkRunnerOptions {
  model?: string;
  maxTurns?: number;
  timeoutMs?: number;
}

export class AgentSdkRunner implements AgentRunner {
  constructor(private readonly opts: AgentSdkRunnerOptions = {}) {}

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    // 1) Scrub the env and inject THIS tenant's grant explicitly.
    const childEnv = buildScrubbedEnv(input.grant, process.env);

    // 2) Load the SDK (operator-gated; dynamic so the stub compiles without it).
    const mod = (await import(sdkSpecifier())) as unknown as AgentSdkModule;

    // 3) One design-time session, granted EXACTLY the three author tools via an
    //    in-process MCP server (no shell, no arbitrary net). settingSources:[] so
    //    the SDK ignores ambient ~/.claude / project settings (spike isolation).
    const authorMcp = buildAuthorMcpServer(input.toolset);
    const q = mod.query({
      prompt: `${input.systemPrompt}\n\nAuthor a tool for: ${input.description}`,
      options: {
        env: childEnv,
        model: this.opts.model,
        maxTurns: this.opts.maxTurns ?? 24,
        settingSources: [],
        permissionMode: "bypassPermissions",
        cwd: input.repoDir,
        allowedTools: ["fs-tenant-repo", "validate-tool", "aps-test-run"],
        mcpServers: { author: authorMcp },
      },
    });

    // 4) Drain the session (the model authors + validates + test-runs via the tools).
    for await (const _msg of q) {
      // Streaming is consumed for teardown; message handling/metering is a
      // downstream concern (self-metered from per-response usage - see research doc).
      void _msg;
    }

    // 5) Resolve the tool package the session wrote into the repo, and re-validate.
    const tool = readNewestAuthoredTool(input.repoDir);
    const diagnostics = validateToolPackage(tool);
    if (diagnostics.length > 0) {
      throw new Error(`Agent SDK session produced an invalid tool: ${diagnostics.join("; ")}`);
    }
    const code = tool.entry ? safeRead(join(input.repoDir, tool.entry)) : "";
    return {
      tool,
      code,
      preview: `Tool "${tool.name}" authored via the Agent SDK (engine_op=${tool.engine_op}).`,
      files: tool.entry ? [tool.entry] : [],
    };
  }
}

/**
 * Build the in-process MCP server exposing the three author tools to the SDK.
 * OPERATOR-GATED shape: the concrete SDK MCP builder (createSdkMcpServer/`tool`)
 * is wired at install time; typed `unknown` here so the stub compiles standalone.
 */
function buildAuthorMcpServer(toolset: AgentRunInput["toolset"]): unknown {
  return {
    name: "author",
    tools: {
      "fs-tenant-repo": toolset.fsTenantRepo,
      "validate-tool": toolset.validateTool,
      "aps-test-run": toolset.apsTestRun,
    },
  };
}

function readNewestAuthoredTool(repoDir: string): ToolPackage {
  const toolsRoot = join(repoDir, "tools");
  let newest: { path: string; mtime: number } | null = null;
  for (const entry of readdirSync(toolsRoot, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const manifest = join(toolsRoot, entry.name, "tool.json");
    try {
      const st = statSync(manifest);
      if (!newest || st.mtimeMs > newest.mtime) newest = { path: manifest, mtime: st.mtimeMs };
    } catch {
      // no manifest in this dir - skip
    }
  }
  if (!newest) throw new Error(`no authored tool.json found under ${toolsRoot}`);
  return JSON.parse(readFileSync(newest.path, "utf8")) as ToolPackage;
}

function safeRead(path: string): string {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}
