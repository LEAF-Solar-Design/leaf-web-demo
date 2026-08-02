/**
 * LIVE SpineConverseRunner - the ONLY Anthropic egress on the converse path, behind
 * the SpineConverseRunner port. One run() = one conversational TURN driven through
 * the Agent SDK; the ConverseLoop owns ALL tool semantics (gate checks, event
 * emission, dispatch) - this runner only TRANSPORTS: it mounts the seven spine
 * tools as an in-process MCP server whose handlers delegate to the loop's
 * ToolExecutor, bridges the SDK's canUseTool to the loop's CanUseTool, and
 * relays model text / usage / terminal state as ConverseRunnerEvents.
 *
 * =========================================================================
 * U1 FINDINGS - verified against the INSTALLED SDK type declarations
 * (node_modules/@anthropic-ai/claude-agent-sdk, version 0.3.214, sdk.d.ts):
 *
 * - RESUME: `Options.resume?: string` (sdk.d.ts:1774-1776) - "Session ID to
 *   resume. Loads the conversation history from the specified session." The
 *   new-session UUID arrives on every message's `session_id` and the result
 *   message; sdk.d.ts:697 confirms "Resumable via `query({ options: { resume:
 *   sessionId } })`". Related: `continue?: boolean` (:1358, mutually exclusive
 *   with resume), `forkSession?: boolean` (:1470), `resumeSessionAt?: string`
 *   (:1788). This runner uses `resume` only.
 *
 * - STREAMING: `Options.includePartialMessages?: boolean` (:1601-1604) emits
 *   `SDKPartialAssistantMessage { type: 'stream_event'; event:
 *   BetaRawMessageStreamEvent; ... }` (:4095-4102). REAL per-token text deltas
 *   exist: `event.type === 'content_block_delta'` with `delta.type ===
 *   'text_delta'`. THIS RUNNER USES TRUE STREAMING (stream_event deltas), not
 *   per-message text; full assistant messages are used for usage metering only
 *   and are NOT re-emitted as text (no double delivery).
 *
 * - canUseTool: `(toolName: string, input: Record<string, unknown>, options:
 *   { signal: AbortSignal; toolUseID: string; requestId: string; ... }) =>
 *   Promise<PermissionResult | null>` (:206-266). PermissionResult =
 *   `{ behavior: 'allow'; updatedInput?; ... } | { behavior: 'deny'; message:
 *   string; interrupt?; ... }` (:2087-2099). The loop's 2-arg CanUseTool maps
 *   1:1 onto this (extra SDK context args are dropped; null is never returned).
 *   NOTE: the gate consult itself lives in the LOOP's executor (a GateClient
 *   check precedes EVERY tool execution); the runner's canUseTool bridge only
 *   forwards to the loop's hook, which allowlists the seven spine tools.
 *
 * - createSdkMcpServer: `createSdkMcpServer({ name, version?, instructions?,
 *   tools?: SdkMcpToolDefinition[] })` (:480-501) returns an
 *   `McpSdkServerConfigWithInstance` passed via `Options.mcpServers` (:1681).
 *   Tools are built with `tool(name, description, zodRawShape, handler)`
 *   (:6852); handlers return MCP CallToolResult `{ content: [{ type: 'text',
 *   text }], isError? }`.
 *
 * - RATE LIMITS: `SDKAssistantMessageError` includes 'rate_limit' (and
 *   'overloaded', 'server_error', ...) as of this SDK version (:2866) - the
 *   author runner's original terminal list predates this. Horizon sources:
 *   `SDKRateLimitEvent { type: 'rate_limit_event'; rate_limit_info:
 *   { status: 'allowed'|'allowed_warning'|'rejected'; resetsAt?: number; ... } }`
 *   (:4182-4211, subscription lane) and `SDKAPIRetryMessage { type: 'system';
 *   subtype: 'api_retry'; retry_delay_ms; error_status; error }` (:2807-2817).
 *   Classification here (plan B3): horizon > 300s => llm_quota_exhausted,
 *   else llm_rate_limited.
 *
 * - ENV: `Options.env` REPLACES the subprocess environment entirely
 *   (:1408-1414); buildScrubbedEnv spreads process.env and injects exactly one
 *   grant credential, so PATH/HOME survive and ambient Anthropic identities do
 *   not.
 * =========================================================================
 *
 * INSTANCE DISCIPLINE: constructed PER SESSION (serve wiring builds a fresh
 * runner per turn) and - stronger - this class keeps NO mutable instance state
 * at all: every counter/accumulator lives inside run(), so even a shared
 * instance cannot bleed telemetry across tenants (the usageLog-on-instance
 * hazard of agentSdkRunner.ts:188-190 is structurally impossible here).
 *
 * Secret discipline: the grant is injected into a scrubbed child env only;
 * never logged, never echoed into any event.
 */

import type {
  AgentGrant,
  ConverseRunInput,
  SpineConverseRunner,
  ConverseRunnerEvent,
  ConverseStopReason,
  ConverseTurnUsage,
  SpineToolName,
} from "../index.js";
import { SPINE_TOOL_NAMES } from "../index.js";
import { buildScrubbedEnv } from "./agentSdkRunner.js";
import { skillBundleAttachment } from "./skillBundle.js";
import { grantSecrets, redactSecrets } from "../../redact.js";

// --------------------------------------------------------------------------- //
// Minimal local views of the SDK / zod surfaces we rely on (documented above,
// not exhaustive). Kept local so `tsc --noEmit` never typechecks their .d.ts.
// --------------------------------------------------------------------------- //
type CallToolResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };
interface SdkModule {
  query(args: { prompt: string | AsyncIterable<unknown>; options: Record<string, unknown> }): AsyncIterable<unknown>;
  createSdkMcpServer(opts: Record<string, unknown>): unknown;
  tool(
    name: string,
    description: string,
    inputSchema: Record<string, unknown>,
    handler: (args: Record<string, unknown>, extra: unknown) => Promise<CallToolResult>,
  ): unknown;
}
/** A zod chainable we only ever call .optional() on. */
type Z = { optional(): Z };
interface ZodModule {
  z: {
    string(): Z;
    number(): Z;
    unknown(): Z;
    enum(v: string[]): Z;
    record(inner: Z): Z;
    array(inner: Z): Z;
    object(shape: Record<string, Z>): Z;
    [k: string]: unknown;
  };
}

/** Non-literal dynamic import so tsc treats the module as `any` (compiles w/o it). */
function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

/** The seven spine tools as mounted MCP names (server key "spine"). */
export const SPINE_MCP_TOOL_NAMES = SPINE_TOOL_NAMES.map((n) => `mcp__spine__${n}`);

/** Rate-limit horizon split (plan B3 / wire contract section 3 stop reasons):
 *  a 429 whose retry-after horizon exceeds this is EXHAUSTION (degraded mode),
 *  anything shorter (or unknown) is a transient rate limit the caller may retry. */
export const QUOTA_HORIZON_S = 300;

export function classifyRateLimit(
  retryAfterS: number | null,
): "llm_quota_exhausted" | "llm_rate_limited" {
  return retryAfterS !== null && retryAfterS > QUOTA_HORIZON_S
    ? "llm_quota_exhausted"
    : "llm_rate_limited";
}

/** The SDK cannot resume sessions whose local transcript disappeared on redeploy. */
function isMissingResumeSessionError(message: string): boolean {
  return /\bNo conversation found with session ID:\s*[0-9a-z-]+\b/i.test(message);
}

/** Normalize an SDK resetsAt (epoch seconds OR milliseconds) to epoch seconds. */
function toEpochS(resetsAt: number): number {
  return resetsAt > 1e12 ? resetsAt / 1000 : resetsAt;
}

/** Wire-contract section 5 tool descriptions (what the model sees). */
const TOOL_DESCRIPTIONS: Record<SpineToolName, string> = {
  catalog_search:
    "Search the platform's registered tool catalog. Args {query, k?}. Returns matching tools; the top match includes its params_schema.",
  drawing_state:
    "Read the current drawing's state. Args {what: 'summary'|'versions'|'checkout'}.",
  ask_user:
    "Ask the user one focused question with 2 to 6 choices. After the question is presented, end your turn and wait for the reply.",
  run_capability:
    "Dispatch ONE registered catalog tool as a platform job. Args {tool, params?, dwg?}. Read tools may return their result inline; write tools return a proposal requiring user approval (re-invoke with confirmation_id after approval).",
  job_status: "Check a previously dispatched job. Args {job_id}.",
  author_tool:
    "Request creation of a new tool when nothing in the catalog fits. Args {description, mode?, confirmation_id?}. Re-invoke with the gate-issued confirmation_id after approval.",
  request_publication:
    "Request or resume publication of a durable staged change. Args {change_set_id}. This does not grant approval and never accepts a receipt or confirmation.",
  request_confirmation:
    "Ask the user to explicitly approve something before proceeding. Args {kind, payload?}. After a pending result, summarize and end your turn.",
};

export interface ConverseSdkRunnerOptions {
  /** The tenant's Agent SDK credential (Concern 2). Resolved by the caller per
   *  session; injected into a scrubbed child env, never logged. */
  grant: AgentGrant;
  /** Model id. Default: LEAF_SPINE_MODEL env, else claude-sonnet-5. */
  model?: string;
  /** Wall-clock turn timeout in seconds. Default: LEAF_SPINE_TURN_TIMEOUT_S env, else 120. */
  turnTimeoutS?: number;
  /** Inner SDK turn cap per conversational turn (spend guard). Default 24. */
  maxTurns?: number;
  /** Test seams: module loaders, mocked at the module boundary in unit tests. */
  sdkImport?: () => Promise<unknown>;
  zodImport?: () => Promise<unknown>;
}

export class ConverseSdkRunner implements SpineConverseRunner {
  // NO mutable instance state (see header) - options are readonly.
  private readonly grant: AgentGrant;
  private readonly model: string;
  private readonly turnTimeoutS: number;
  private readonly maxTurns: number;
  private readonly sdkImport: () => Promise<unknown>;
  private readonly zodImport: () => Promise<unknown>;

  constructor(opts: ConverseSdkRunnerOptions) {
    this.grant = opts.grant;
    this.model = opts.model ?? process.env.LEAF_SPINE_MODEL ?? "claude-sonnet-5";
    this.turnTimeoutS =
      opts.turnTimeoutS ?? (Number(process.env.LEAF_SPINE_TURN_TIMEOUT_S || "") || 120);
    this.maxTurns = opts.maxTurns ?? 24;
    this.sdkImport = opts.sdkImport ?? (() => dynImport(["@anthropic-ai", "claude-agent-sdk"]));
    this.zodImport = opts.zodImport ?? (() => dynImport(["zod"]));
  }

  /**
   * Public entry: NOTHING carrying this run's credential may escape here.
   *
   * This runner is the only component that holds the grant. ConverseLoop, which
   * catches whatever escapes, is deliberately credential-free (enforced by the
   * import invariant in test/converseRuntimeSeparation.test.ts) and so cannot
   * scrub by value — and its catch writes to the DURABLE transcript via
   * store.appendEvent BEFORE any wire-level filtering runs. So the scrub has to
   * happen here, on the way out. (sol-critic PR #117 round 2, blocker 1.)
   */
  async *run(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent> {
    try {
      yield* this.runInner(input);
    } catch (e) {
      const scrubbed = redactSecrets(
        e instanceof Error ? e.message : String(e),
        grantSecrets(this.grant),
      );
      const wrapped = new Error(scrubbed);
      // Drop the original stack: it can quote the offending value too.
      wrapped.stack = `${wrapped.name}: ${scrubbed}`;
      throw wrapped;
    }
  }

  private async *runInner(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent> {
    const sdk = (await this.sdkImport()) as SdkModule;
    const { z } = (await this.zodImport()) as ZodModule;

    // Scrubbed env with EXACTLY this tenant's grant injected (never logged).
    const childEnv = buildScrubbedEnv(this.grant, process.env);

    // The seven spine tools, BRIDGED to the loop's executor. The loop owns the
    // semantics (gate check, wire events, dispatch); handlers only transport.
    const toResult = (r: { content: string; isError?: boolean }): CallToolResult => ({
      content: [{ type: "text", text: r.content }],
      ...(r.isError ? { isError: true } : {}),
    });
    const schemas: Record<SpineToolName, Record<string, unknown>> = {
      catalog_search: { query: z.string(), k: z.number().optional() },
      drawing_state: { what: z.enum(["summary", "versions", "checkout"]) },
      ask_user: {
        question: z.string(),
        options: z.array(z.object({ label: z.string(), description: z.string().optional() })),
      },
      run_capability: {
        tool: z.string(),
        params: z.record(z.unknown()).optional(),
        dwg: z.string().optional(),
        confirmation_id: z.string().optional(),
      },
      job_status: { job_id: z.string() },
      author_tool: {
        description: z.string(),
        mode: z.enum(["build", "one_off"]).optional(),
        target_tool_name: z.string().optional(),
        confirmation_id: z.string().optional(),
      },
      request_publication: { change_set_id: z.string() },
      request_confirmation: { kind: z.string(), payload: z.record(z.unknown()).optional() },
    };
    const tools = SPINE_TOOL_NAMES.map((name) =>
      sdk.tool(name, TOOL_DESCRIPTIONS[name], schemas[name], async (args) =>
        toResult(await input.tools.execute(name, args)),
      ),
    );
    const server = sdk.createSdkMcpServer({ name: "spine", version: "1.0.0", tools });

    // WALL-CLOCK timeout: no single turn may hold its session lock forever.
    const abort = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      abort.abort();
    }, this.turnTimeoutS * 1000);

    const model = input.model ?? this.model;
    /**
     * A text turn stays a plain string, byte-identical to before. A turn with
     * images becomes the SDK's structured user message: image blocks first,
     * then the text block, the shape the vision spike proved reaches the model.
     *
     * Built fresh on every call, never hoisted into a variable. An async
     * generator is consumed ONCE, and this runner re-queries on a missing SDK
     * transcript — a hoisted generator would replay as an EMPTY prompt on that
     * retry, silently dropping both the image and the question.
     */
    const withImages = (text: string): string | AsyncIterable<unknown> => {
      if (!input.images?.length) return text;
      const content = [
        ...input.images.map((image) => ({
          type: "image",
          source: { type: "base64", media_type: image.media_type, data: image.data },
        })),
        { type: "text", text },
      ];
      return (async function* () {
        yield { type: "user", message: { role: "user", content }, parent_tool_use_id: null };
      })();
    };
    // A bundle reaches the SDK only after its complete inventory is verified
    // against a REQUIRED deployment digest pin, and what mounts is a private
    // normalised snapshot of the verified bytes, never the source directory.
    const skillBundle = skillBundleAttachment();
    const query = (prompt: string, resume?: string): AsyncIterable<unknown> =>
      sdk.query({
        prompt: withImages(prompt),
        options: {
          env: childEnv,
          model,
          systemPrompt: input.systemPrompt,
          maxTurns: this.maxTurns,
          settingSources: [],
          permissionMode: "default",
          abortController: abort,
          includePartialMessages: true,
          tools: [], // no built-in tools: the spine MCP server is the whole surface
          mcpServers: { spine: server },
          ...(skillBundle ? { plugins: [skillBundle.plugin], skills: skillBundle.skills } : {}),
          // SETTINGS, not top-level options — the distinction is the whole
          // fix. Both flags below are Settings fields (sdk.d.ts) delivered
          // through Options.settings, which the SDK renders as `--settings`.
          // Passing them at the top level silently does NOTHING: a process
          // probe shows the CLI receiving `--allowedTools Skill(...)` and no
          // `--settings` at all. My first attempt did exactly that and "pinned"
          // it with a test that inspected the mocked options object, so the
          // test passed while the guard was absent from the real command line.
          //
          // WHY BOTH ARE REQUIRED, and why canUseTool is not the containment:
          // the SDK compiles `skills: [name]` into `--allowedTools Skill(name)`,
          // and allowlisted tools execute WITHOUT consulting canUseTool. So a
          // mounted bundle could otherwise reach execution two ways —
          //   * inline shell commands inside a skill  -> disableSkillShellExecution
          //   * plugin/skill HOOKS, which spawn commands on their own -> disableAllHooks
          // Both are outside submitRun and outside the app gate, on the LIVE
          // path. With them set, a mounted bundle is INSTRUCTIONS ONLY, which
          // is all a curated bundle is for.
          settings: {
            disableSkillShellExecution: true,
            disableAllHooks: true,
          },
          // WHY THE MOUNT ABOVE IS SAFE. Handing the SDK a plugin directory
          // is how a skill reaches execution outside canUseTool and the app
          // gate, and successive review rounds each found a fresh route:
          // inline skill shell commands, plugin and skill hooks, `context:
          // fork` spawning a subagent, plugin MONITORS in plugin.json, nested
          // payloads, a YAML-quoted key. Patching each one was losing, because
          // inspect-and-denylist is the wrong model for this surface.
          //
          // What ships instead is allowlist-by-verification: the bundle is
          // hashed against its manifest under a required deployment pin, and
          // what the SDK actually loads is a private snapshot we write from
          // those verified bytes, with frontmatter we author containing only
          // name and description. Nothing the source declared survives it.
          //
          // The two settings above stay regardless: they are cheap, they only
          // narrow, and they are correct for a spine session either way.
          ...(resume ? { resume } : {}),
          canUseTool: async (toolName: string, inp: Record<string, unknown>) =>
            // Bridge to the loop's hook (allow spine tools / deny everything else).
            input.canUseTool(toolName, inp),
        },
      });

    // Per-RUN accumulators (never instance state).
    let turns = 0;
    const sums = { input_tokens: 0, output_tokens: 0, cache_creation_tokens: 0, cache_read_tokens: 0 };
    const models = new Set<string>();
    let sdkSessionId: string | null = input.resumeSdkSessionId ?? null;
    let result: Record<string, unknown> | null = null;
    let terminalError: string | null = null;
    let rateResetsAtS: number | null = null;
    let retryDelayMs: number | null = null;
    let streamFault: string | null = null;
    let sdkSessionReset = false;
    let attemptProducedOutput = false;

    try {
      let prompt = input.userMessage;
      let resume = input.resumeSdkSessionId;
      while (true) {
        try {
          for await (const raw of query(prompt, resume)) {
            const msg = raw as Record<string, unknown>;
            if (typeof msg.session_id === "string" && msg.session_id) {
              sdkSessionId = msg.session_id;
            }

            if (msg.type === "stream_event") {
              // TRUE streaming (see U1 header): only content_block_delta text_delta
              // becomes wire text - assistant messages are metering-only.
              const ev = (msg.event ?? {}) as Record<string, unknown>;
              if (ev.type === "content_block_delta") {
                const delta = (ev.delta ?? {}) as Record<string, unknown>;
                if (delta.type === "text_delta" && typeof delta.text === "string" && delta.text) {
                  attemptProducedOutput = true;
                  yield { type: "text_delta", text: delta.text };
                }
              }
            } else if (msg.type === "assistant") {
              attemptProducedOutput = true;
              turns += 1;
              const message = (msg.message ?? {}) as Record<string, unknown>;
              const u = (message.usage ?? {}) as Record<string, number>;
              sums.input_tokens += u.input_tokens ?? 0;
              sums.output_tokens += u.output_tokens ?? 0;
              sums.cache_creation_tokens += u.cache_creation_input_tokens ?? 0;
              sums.cache_read_tokens += u.cache_read_input_tokens ?? 0;
              if (typeof message.model === "string") models.add(message.model);
              const err = msg.error;
              if (
                typeof err === "string" &&
                ["rate_limit", "authentication_failed", "oauth_org_not_allowed", "billing_error"].includes(err)
              ) {
                terminalError = err;
                abort.abort();
                break;
              }
            } else if (msg.type === "rate_limit_event") {
              const info = (msg.rate_limit_info ?? {}) as Record<string, unknown>;
              if (typeof info.resetsAt === "number") rateResetsAtS = toEpochS(info.resetsAt);
            } else if (msg.type === "system") {
              if (msg.subtype === "api_retry" && typeof msg.retry_delay_ms === "number") {
                retryDelayMs = msg.retry_delay_ms;
              }
            } else if (msg.type === "result") {
              result = msg;
            }
          }
          break;
        } catch (e) {
          const message = (e as Error).message;
          if (resume && !attemptProducedOutput && isMissingResumeSessionError(message)) {
            sdkSessionReset = true;
            sdkSessionId = null;
            resume = undefined;
            prompt = input.resumeFallbackUserMessage ?? input.userMessage;
            continue;
          }
          throw e;
        }
      }
    } catch (e) {
      // An aborted SDK stream throws; our own aborts (timeout / terminal error)
      // are classified below, anything else is a genuine stream fault.
      //
      // REDACT before this string escapes. Unlike the stderr sites, this message
      // becomes a DURABLE error event: the app relays it into the transcript and
      // out over SSE (server/turn_runner.py append_event). A grant is injected
      // into the runner env and can be embedded verbatim in a thrown message
      // (e.g. Node/undici header-validation errors quote the offending value),
      // so an unredacted fault would persist and publish the caller's token.
      //
      // Scrub THIS RUN'S ACTUAL grant value, not just token-shaped text: a
      // credential can clear the app's 24-char floor while still being too short
      // for TOKENISH (which wants sk-ant-* or 40+ chars), so the pattern pass
      // alone would leak it. This is also the ONE place the tenant's LINKED
      // grant can be stripped — the app never sees that value, so its own
      // input-side scrub cannot cover it.
      // (sol-critic PR #117 round 1 blocker 2, round 2 blocker 1.)
      if (!timedOut && !terminalError) {
        streamFault = redactSecrets((e as Error).message, grantSecrets(this.grant));
      }
    } finally {
      clearTimeout(timer);
    }

    // Usage (authoritative totals from the result message when present).
    const rUsage = (result?.usage ?? {}) as Record<string, number>;
    const inputTokens = rUsage.input_tokens ?? sums.input_tokens;
    const outputTokens = rUsage.output_tokens ?? sums.output_tokens;
    const cacheCreation = rUsage.cache_creation_input_tokens ?? sums.cache_creation_tokens;
    const cacheRead = rUsage.cache_read_input_tokens ?? sums.cache_read_tokens;
    const modelUsage = (result?.modelUsage ?? {}) as Record<string, unknown>;
    for (const m of Object.keys(modelUsage)) models.add(m);
    const usage: ConverseTurnUsage = {
      turns: typeof result?.num_turns === "number" ? (result.num_turns as number) : turns,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
      cache_creation_tokens: cacheCreation,
      cache_read_tokens: cacheRead,
      // Cost-relevant tokens = input + output + cache_creation (cache_read excluded).
      cost_tokens: inputTokens + outputTokens + cacheCreation,
      ...(typeof result?.total_cost_usd === "number"
        ? { total_cost_usd: result.total_cost_usd as number }
        : {}),
      ...(models.size > 0 ? { models: [...models] } : {}),
    };
    yield { type: "usage", usage };

    // Terminal state.
    let stopReason: ConverseStopReason;
    let error: { error_code: string; message: string; retryable: boolean; retry_after_s?: number } | undefined;
    if (timedOut) {
      stopReason = "timeout";
      error = {
        error_code: "timeout",
        message: `turn exceeded the ${this.turnTimeoutS}s wall-clock budget and was aborted`,
        retryable: true,
      };
    } else if (terminalError === "rate_limit") {
      const nowS = Date.now() / 1000;
      const retryAfterS =
        rateResetsAtS !== null
          ? Math.max(0, rateResetsAtS - nowS)
          : retryDelayMs !== null
            ? retryDelayMs / 1000
            : null;
      stopReason = classifyRateLimit(retryAfterS);
      error = {
        error_code: stopReason,
        message:
          `Anthropic rate limited` +
          (retryAfterS !== null ? ` (retry after ~${Math.round(retryAfterS)}s)` : " (horizon unknown)"),
        retryable: true,
        ...(retryAfterS !== null ? { retry_after_s: retryAfterS } : {}),
      };
    } else if (terminalError) {
      stopReason = "error";
      error = {
        error_code: terminalError,
        message: `Agent SDK terminal failure: ${terminalError}`,
        retryable: false,
      };
    } else if (streamFault) {
      stopReason = "error";
      error = { error_code: "internal", message: streamFault, retryable: false };
    } else if (result && result.subtype === "success") {
      stopReason = "end_turn";
    } else if (result && result.subtype === "error_max_turns") {
      stopReason = "cap_hit";
      error = {
        error_code: "cap_hit",
        message: `turn hit the ${this.maxTurns}-SDK-turn spend cap`,
        retryable: false,
      };
    } else {
      stopReason = "error";
      error = {
        error_code: "internal",
        message: `SDK session ended without a success result (subtype=${String(result?.subtype ?? "none")})`,
        retryable: false,
      };
    }

    yield {
      type: "done",
      stopReason,
      sdkSessionId,
      ...(sdkSessionReset ? { sdkSessionReset: true } : {}),
      ...(error ? { error } : {}),
    };
  }
}
