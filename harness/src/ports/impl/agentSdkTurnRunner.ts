/**
 * REAL ConverseRunner - the Agent SDK loop boundary for the sessions/turn-engine wire
 * (leaf-backend-gaps.md §2.1, ports/converse.ts FROZEN). This is the riskiest lane in
 * the sessions build: everything else (turn engine, store, approvals router) runs
 * against a scripted fake; this file is what eventually talks to the real SDK.
 *
 * === SDK reality, read from node_modules/@anthropic-ai/claude-agent-sdk/*.d.ts
 * (v0.3.214) before writing a line of mapping code below - not assumed ===
 *
 *  - `query({prompt, options}): Query` where `Query extends AsyncGenerator<SDKMessage,
 *    void>` - a big discriminated union. The subset this runner cares about:
 *
 *      SDKAssistantMessage  { type:'assistant', message: BetaMessage, error?:
 *        SDKAssistantMessageError }
 *        `message.content: BetaContentBlock[]` - the two block kinds we act on:
 *          BetaTextBlock    { type:'text', text: string }
 *          BetaToolUseBlock { type:'tool_use', id: string, name: string, input: unknown }
 *        `message.usage` carries the per-turn token counts (same shape AgentSdkRunner
 *        already meters from).
 *
 *      SDKUserMessage { type:'user', message: MessageParam }
 *        This is how a tool's result comes back through the SAME async iterable (the
 *        SDK internally appends it as a "user" turn before continuing). Its
 *        `message.content` MAY be an array containing a tool_result block:
 *          BetaToolResultBlockParam { type:'tool_result', tool_use_id: string,
 *            content?: string | Array<{type:'text', text:string, ...}>, is_error?: bool }
 *        Crucially the block carries `tool_use_id`, never the tool NAME - the name has
 *        to be correlated back to the earlier `tool_use` block's `id`. Hence
 *        `mapSdkMessage`'s second (mutable, caller-owned) `toolUseNames` argument.
 *
 *      SDKResultMessage = SDKResultSuccess | SDKResultError
 *        Both variants carry `subtype`, `usage: NonNullableUsage`, `total_cost_usd:
 *        number`, `stop_reason: string | null`; the error variant adds `errors:
 *        string[]`. `subtype` is one of 'success' | 'error_max_turns' |
 *        'error_max_budget_usd' | 'error_during_execution' |
 *        'error_max_structured_output_retries'. This is the ONE terminal message the
 *        SDK is documented to always eventually produce for a completed (non-aborted)
 *        turn - `stopReasonFromResult` below classifies it into our wire StopReason.
 *
 *      SDKPartialAssistantMessage { type:'stream_event', event: BetaRawMessageStreamEvent }
 *        Only emitted when `Options.includePartialMessages` is set. This runner does
 *        NOT set it (keeps the loop simple and matches AgentSdkRunner's precedent), so
 *        true token-level streaming is never surfaced here - `mapSdkMessage` therefore
 *        CHUNKS each finished assistant text block into synthetic `text_delta` events
 *        instead of relaying a single big block (a real client still gets a "typing"
 *        stream, just coarser-grained than true SDK streaming would give).
 *
 *  - `CanUseTool = (toolName, input, options) => Promise<PermissionResult | null>`,
 *    `PermissionResult = {behavior:'allow', updatedInput?, ...} | {behavior:'deny',
 *    message, interrupt?, ...}` - confirms the `{behavior:'allow'|'deny', ...}` shape
 *    AgentSdkRunner.ts:334-340 already uses. `toolName` for an SDK-MCP-server-defined
 *    tool arrives PREFIXED (`mcp__<server>__<tool>`); this runner strips that prefix
 *    before it ever reaches a wire event or the `approvalTools` allow-list.
 *
 * The SDK (and zod) are loaded via a NON-LITERAL dynamic import, same discipline as
 * AgentSdkRunner.ts, so `tsc --noEmit` over src/ never has to load their (huge) type
 * graphs and this file compiles without `npm i @anthropic-ai/claude-agent-sdk` too.
 *
 * === What is genuinely gated behind the live SDK (untested here by design) ===
 * `mapSdkMessage` (+ its small pure helpers) is the CORE, exhaustively fixture-tested
 * by test/turnMapping.test.ts - zero SDK/network calls. `AgentSdkTurnRunner.runTurn`
 * wires that pure mapper to a real `sdk.query()` session; per the lane brief ("you gate
 * only the final live smoke") it is exercised by a live smoke test elsewhere, never by
 * this hermetic suite - invoking it here would spend real LLM credit.
 */

import { randomUUID } from "node:crypto";
import type {
  AgentGrant,
  BrokerApsClient,
  ConverseRunner,
  ConverseTurnInput,
  HarnessTurnEvent,
  OAuthGrantProvider,
  ResultEnvelope,
  StopReason,
  TenantRepoProvider,
} from "../index.js";
import { buildScrubbedEnv } from "./agentSdkRunner.js";
import { findTool } from "../../registry/registerTool.js";

// --------------------------------------------------------------------------- //
// Minimal local views of the SDK / zod surfaces this file relies on (documented above,
// not exhaustive). Kept local, same as AgentSdkRunner.ts, so tsc never typechecks
// against the real .d.ts graph.
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
    [k: string]: unknown;
  };
}

/** Non-literal dynamic import so tsc treats the module as `any` (compiles w/o it). */
function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

// =========================================================================== //
// PURE mapping: one raw SDK message -> zero or more wire HarnessTurnEvents.
//
// "Pure" here means: no I/O, no module-level mutable state, deterministic given its
// arguments. The one piece of cross-message state a converse turn genuinely needs -
// which tool NAME a later tool_result's `tool_use_id` belongs to - is threaded through
// explicitly as the second argument (a Map the CALLER owns for the lifetime of one
// turn) rather than hidden behind a closure, so every case is independently
// constructible and assertable in a test.
// =========================================================================== //

const TEXT_DELTA_CHUNK_CHARS = 48;
const ARGS_SUMMARY_MAX_CHARS = 300;
const RESULT_SUMMARY_MAX_CHARS = 500;

/** Assistant-turn error codes that map onto a specific wire StopReason instead of a
 *  generic `error` event (SDKAssistantMessageError, sdk.d.ts:2866). */
const RATE_LIMIT_ASSISTANT_ERRORS = new Set(["rate_limit", "overloaded"]);
const QUOTA_ASSISTANT_ERRORS = new Set(["billing_error"]);

/**
 * Split `text` into ordered chunks whose concatenation reconstructs it EXACTLY
 * (round-trips - see test/turnMapping.test.ts). Breaks on the last whitespace at or
 * before the chunk boundary when one exists, else hard-cuts, so a client rendering
 * each chunk as it arrives sees a natural word-at-a-time "typing" effect. Empty input
 * yields zero chunks (no empty `text_delta` is ever emitted).
 */
export function chunkText(text: string): string[] {
  if (!text) return [];
  const chunks: string[] = [];
  let rest = text;
  while (rest.length > TEXT_DELTA_CHUNK_CHARS) {
    let cut = rest.lastIndexOf(" ", TEXT_DELTA_CHUNK_CHARS);
    if (cut <= 0) cut = TEXT_DELTA_CHUNK_CHARS;
    chunks.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest.length) chunks.push(rest);
  return chunks;
}

/** Short, log/UI-friendly rendering of a tool_use block's `input` for `args_summary`. */
export function summarizeArgs(input: unknown): string {
  let text: string;
  try {
    text = JSON.stringify(input) ?? "null";
  } catch {
    text = String(input);
  }
  return text.length > ARGS_SUMMARY_MAX_CHARS
    ? `${text.slice(0, ARGS_SUMMARY_MAX_CHARS)}… (truncated)`
    : text;
}

/** Flatten a tool_result block's `content` (string, or array of {text} blocks per
 *  BetaToolResultBlockParam) into one short human-readable summary string. */
export function summarizeToolResultContent(content: unknown): string {
  let text: string;
  if (typeof content === "string") {
    text = content;
  } else if (Array.isArray(content)) {
    text = content
      .map((b) => {
        const block = b as Record<string, unknown>;
        return typeof block.text === "string" ? block.text : "";
      })
      .filter((s) => s.length > 0)
      .join("\n");
  } else {
    text = "";
  }
  return text.length > RESULT_SUMMARY_MAX_CHARS
    ? `${text.slice(0, RESULT_SUMMARY_MAX_CHARS)}… (truncated)`
    : text;
}

/** Classify a terminal SDKResultMessage into the wire StopReason enum. */
export function stopReasonFromResult(result: Record<string, unknown>): StopReason {
  const subtype = result.subtype;
  if (subtype === "success") return "end_turn";
  if (
    subtype === "error_max_turns" ||
    subtype === "error_max_budget_usd" ||
    subtype === "error_max_structured_output_retries"
  ) {
    return "cap_hit";
  }
  const errs = Array.isArray(result.errors) ? (result.errors as unknown[]).join(" ") : "";
  const stop = typeof result.stop_reason === "string" ? result.stop_reason : "";
  const haystack = `${errs} ${stop}`.toLowerCase();
  if (/rate.?limit|\b429\b|overloaded/.test(haystack)) return "llm_rate_limited";
  if (/quota|credit|billing/.test(haystack)) return "llm_quota_exhausted";
  return "error";
}

function mapAssistant(msg: Record<string, unknown>, toolUseNames: Map<string, string>): HarnessTurnEvent[] {
  const err = msg.error;
  if (typeof err === "string") {
    if (RATE_LIMIT_ASSISTANT_ERRORS.has(err)) {
      return [{ type: "turn_complete", data: { stop_reason: "llm_rate_limited" } }];
    }
    if (QUOTA_ASSISTANT_ERRORS.has(err)) {
      return [{ type: "turn_complete", data: { stop_reason: "llm_quota_exhausted" } }];
    }
    return [
      { type: "error", data: { error: { error_code: err, message: `Agent SDK turn failed: ${err}` } } },
      { type: "turn_complete", data: { stop_reason: "error" } },
    ];
  }

  const message = (msg.message ?? {}) as Record<string, unknown>;
  const content = Array.isArray(message.content) ? (message.content as unknown[]) : [];
  const events: HarnessTurnEvent[] = [];
  for (const raw of content) {
    const block = raw as Record<string, unknown>;
    if (block.type === "text" && typeof block.text === "string") {
      for (const chunk of chunkText(block.text)) {
        events.push({ type: "text_delta", data: { text: chunk } });
      }
    } else if (block.type === "tool_use" && typeof block.id === "string" && typeof block.name === "string") {
      toolUseNames.set(block.id, block.name);
      events.push({ type: "tool_call", data: { tool: block.name, args_summary: summarizeArgs(block.input) } });
    }
    // Other block kinds (thinking, server_tool_use, ...) carry no event in the frozen
    // 9-type ConverseRunner set - intentionally dropped.
  }
  return events;
}

function mapUser(msg: Record<string, unknown>, toolUseNames: Map<string, string>): HarnessTurnEvent[] {
  const message = (msg.message ?? {}) as Record<string, unknown>;
  const content = message.content;
  if (!Array.isArray(content)) return [];
  const events: HarnessTurnEvent[] = [];
  for (const raw of content) {
    const block = raw as Record<string, unknown>;
    if (block.type !== "tool_result") continue;
    const toolUseId = typeof block.tool_use_id === "string" ? block.tool_use_id : "";
    const tool = toolUseNames.get(toolUseId) ?? "unknown";
    const ok = block.is_error !== true;
    events.push({ type: "tool_result", data: { tool, ok, summary: summarizeToolResultContent(block.content) } });
  }
  return events;
}

function mapResult(msg: Record<string, unknown>): HarnessTurnEvent[] {
  const usage = (msg.usage ?? {}) as Record<string, number>;
  // Cost-relevant tokens = input + output + cache_creation, EXCLUDING cache_read - same
  // arithmetic AgentSdkRunner.ts:354-384 uses for its spend-cap accounting.
  const cost_tokens =
    (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0);
  const usageData: Record<string, unknown> = { cost_tokens };
  if (typeof msg.total_cost_usd === "number") usageData.total_cost_usd = msg.total_cost_usd;
  return [
    { type: "turn_usage", data: usageData },
    { type: "turn_complete", data: { stop_reason: stopReasonFromResult(msg) } },
  ];
}

/**
 * Map ONE raw SDK message to zero or more wire events. `toolUseNames` is the running
 * tool_use_id -> tool_name correlation table for THIS turn; pass the SAME Map instance
 * across every message of one turn (a fresh Map per turn - never module-level state).
 * Unrecognized/irrelevant SDK message types (system/*, tool_progress, stream_event,
 * auth_status, ...) map to `[]` - a forward-compatible no-op, not an error.
 */
export function mapSdkMessage(raw: unknown, toolUseNames: Map<string, string> = new Map()): HarnessTurnEvent[] {
  const msg = raw as Record<string, unknown> | null | undefined;
  if (!msg || typeof msg !== "object") return [];
  switch (msg.type) {
    case "assistant":
      return mapAssistant(msg, toolUseNames);
    case "user":
      return mapUser(msg, toolUseNames);
    case "result":
      return mapResult(msg);
    default:
      return [];
  }
}

// =========================================================================== //
// The real runner. Untested by the hermetic suite (would spend LLM credit / touch the
// network) - correctness here rests on (a) mapSdkMessage above being exhaustively
// fixture-tested, and (b) the live smoke gate the lane brief calls out separately.
// =========================================================================== //

const MCP_SERVER_NAME = "converse";
const MCP_TOOL_PREFIX = `mcp__${MCP_SERVER_NAME}__`;
const APS_TEST_RUN_TOOL = "aps_test_run";
const APS_TEST_RUN_MCP_NAME = `${MCP_TOOL_PREFIX}${APS_TEST_RUN_TOOL}`;

/** Tools that mutate/spend real resources and therefore require operator approval
 *  before they run (leaf-backend-gaps.md §2.1: "aps_test_run / anything mutating"). */
const DEFAULT_APPROVAL_TOOLS = new Set<string>([APS_TEST_RUN_TOOL]);

function bareToolName(name: string): string {
  return name.startsWith(MCP_TOOL_PREFIX) ? name.slice(MCP_TOOL_PREFIX.length) : name;
}

/** Parse the wrapper's `params_json` field (see makeApsTestRun / apsTestRun.ts: the
 *  inner target tool's params travel inside `aps_test_run`'s call args as a JSON
 *  string, not as the wrapper's own params). Malformed/absent -> {}, never throws. */
function parseWrapperParams(paramsJson: unknown): Record<string, unknown> {
  if (typeof paramsJson !== "string" || !paramsJson.trim()) return {};
  try {
    return JSON.parse(paramsJson) as Record<string, unknown>;
  } catch {
    return {};
  }
}

/** Resolve the ACTUAL target tool + params from the `aps_test_run` wrapper's own call
 *  args (`inp: {tool, params_json}` - see apsTestRun.ts). Used both when the interception
 *  in `canUseTool` records the proposal (FINDING 2: the proposal must carry the target,
 *  not the wrapper, or resume looks up "aps_test_run" in the tenant registry and fails
 *  to find it) and by the `aps_test_run` handler itself. Exported standalone (pure, no
 *  SDK dependency) so the hermetic suite can assert the interception logic directly. */
export function resolveWrapperTarget(
  bareWrapperName: string,
  inp: Record<string, unknown>,
): { tool: string; params: Record<string, unknown> } {
  const targetTool = typeof inp.tool === "string" && inp.tool.trim() ? inp.tool : undefined;
  return { tool: targetTool ?? bareWrapperName, params: parseWrapperParams(inp.params_json) };
}

/** Rewrite an event's `data.tool` from the wire-prefixed MCP name to the bare tool
 *  name a client should render (mapSdkMessage itself stays server-name-agnostic). */
function debarify(ev: HarnessTurnEvent): HarnessTurnEvent {
  if ((ev.type === "tool_call" || ev.type === "tool_result") && typeof ev.data.tool === "string") {
    return { ...ev, data: { ...ev.data, tool: bareToolName(ev.data.tool) } };
  }
  return ev;
}

type ConfirmProposal = NonNullable<ConverseTurnInput["confirm"]>["proposal"];

export interface AgentSdkTurnRunnerPorts {
  oauth: OAuthGrantProvider;
  tenantRepo: TenantRepoProvider;
  broker: BrokerApsClient;
}

export interface AgentSdkTurnRunnerOptions {
  /** undefined => the SDK/account default model. */
  model?: string;
  /** Spend cap: abort past this many SDK-internal assistant turns within ONE converse turn. */
  maxTurns?: number;
  /** Spend cap: abort past this many cost-relevant tokens within ONE converse turn. */
  maxTotalTokens?: number;
  /** Bare tool names that require operator confirmation before they run. Defaults to
   *  {"aps_test_run"}. */
  approvalTools?: Set<string>;
}

function summarizeEnvelope(envelope: ResultEnvelope): string {
  if (envelope.ok) {
    const text = JSON.stringify(envelope.result ?? {});
    return text.length > RESULT_SUMMARY_MAX_CHARS ? `${text.slice(0, RESULT_SUMMARY_MAX_CHARS)}… (truncated)` : text;
  }
  return envelope.error?.message ?? "the tool run failed with no error detail.";
}

/** Best-effort job id extraction. ResultEnvelope (CONTRACT section 3) has no dedicated
 *  `job_id` field today, so `job_linked` is only emitted when the broker's result
 *  payload happens to carry one - forward-compatible, never fabricated. */
function extractJobId(envelope: ResultEnvelope): string | undefined {
  const result = envelope.result as Record<string, unknown> | undefined;
  const candidate = result?.job_id;
  return typeof candidate === "string" ? candidate : undefined;
}

export class AgentSdkTurnRunner implements ConverseRunner {
  constructor(
    private readonly ports: AgentSdkTurnRunnerPorts,
    private readonly opts: AgentSdkTurnRunnerOptions = {},
  ) {}

  async *runTurn(input: ConverseTurnInput): AsyncIterable<HarnessTurnEvent> {
    // Resolve the tenant's Agent SDK grant FIRST, before any yield: a thrown
    // GrantRequiredError propagates un-caught out of this generator's first next()
    // call, so the HTTP shell (server.ts /turn route) sees it before any NDJSON output
    // has started and can answer with a clean non-stream 401 (converse.ts doc: "401
    // {grant_required:true,...}") - the SAME mechanism /author already relies on.
    const grant = await this.ports.oauth.getGrant(input.tenant_id);

    if (input.confirm) {
      yield* this.runConfirm(input, grant);
      return;
    }
    yield* this.driveSession(input, grant);
  }

  private async *runConfirm(
    input: ConverseTurnInput,
    grant: AgentGrant,
  ): AsyncGenerator<HarnessTurnEvent> {
    const { approved, proposal } = input.confirm!;
    if (!approved) {
      yield { type: "text_delta", data: { text: "Okay, I won't run that." } };
      yield { type: "turn_complete", data: { stop_reason: "end_turn" } };
      return;
    }

    const outcome = await this.executeProposal(input, proposal);
    yield { type: "tool_result", data: { tool: proposal.tool, ok: outcome.ok, summary: outcome.summary } };
    if (outcome.jobId) {
      yield { type: "job_linked", data: { job_id: outcome.jobId, tool: proposal.tool } };
    }

    // Continue the conversation with the executed result folded into context so the
    // model can report on it / keep going - a FRESH sdk.query() session (the one that
    // halted on confirmation_required cannot be resumed in place; it was aborted).
    const continuationMessages = [
      ...input.messages,
      { role: "assistant" as const, text: `Ran "${proposal.tool}". Result: ${outcome.summary}` },
    ];
    yield* this.driveSession({ ...input, messages: continuationMessages }, grant);
  }

  private async *driveSession(
    input: ConverseTurnInput,
    grant: AgentGrant,
  ): AsyncGenerator<HarnessTurnEvent> {
    const maxTurns = this.opts.maxTurns ?? 24;
    const maxTotalTokens = this.opts.maxTotalTokens ?? 500_000;
    const approvalTools = this.opts.approvalTools ?? DEFAULT_APPROVAL_TOOLS;

    const childEnv = buildScrubbedEnv(grant, process.env);
    const sdk = (await dynImport(["@anthropic-ai", "claude-agent-sdk"])) as unknown as SdkModule;
    const { z } = (await dynImport(["zod"])) as unknown as ZodModule;

    const toolUseNames = new Map<string, string>();
    const pending: HarnessTurnEvent[] = [];
    const abort = new AbortController();
    let awaitingApproval = false;

    const apsTestRunTool = sdk.tool(
      APS_TEST_RUN_TOOL,
      "Run a registered tool on the tenant's current drawing through the broker and return the section-3 result envelope. Requires operator approval before it executes.",
      {
        tool: z.string(),
        params_json: (z.string() as { optional(): unknown }).optional(),
      },
      async (a): Promise<CallToolResult> => {
        // Structurally unreachable while `aps_test_run` stays in `approvalTools`
        // (canUseTool below denies it before the SDK ever calls this handler); kept so
        // a future non-gated tool sharing this shape has a correct handler to reuse.
        const toolName = String(a.tool ?? "");
        const params = parseWrapperParams(a.params_json);
        const outcome = await this.executeProposal(input, { tool: toolName, params });
        return outcome.ok
          ? { content: [{ type: "text", text: outcome.summary }] }
          : { content: [{ type: "text", text: outcome.summary }], isError: true };
      },
    );

    const server = sdk.createSdkMcpServer({ name: MCP_SERVER_NAME, version: "1.0.0", tools: [apsTestRunTool] });

    const canUseTool = async (
      toolName: string,
      inp: Record<string, unknown>,
    ): Promise<{ behavior: string; message?: string; interrupt?: boolean; updatedInput?: Record<string, unknown> }> => {
      const bare = bareToolName(toolName);
      if (!approvalTools.has(bare)) return { behavior: "allow", updatedInput: inp };

      // `inp` here is the WRAPPER's (aps_test_run) own call args - {tool, params_json}
      // - not the tool the user actually wants to run. The proposal (and therefore the
      // resume/confirm payload the caller echoes back on approval) must carry the
      // TARGET tool + its parsed params, or resume looks up "aps_test_run" itself in
      // the tenant registry instead of e.g. "count-by-layer" and fails to find it.
      const confirmationId = randomUUID();
      const { tool: proposalTool, params: targetParams } = resolveWrapperTarget(bare, inp);
      const capability = await this.capabilityFor(input.tenant_id, proposalTool === bare ? undefined : proposalTool);
      pending.push({
        type: "proposed_run",
        data: {
          confirmation_id: confirmationId,
          tool: proposalTool,
          params: targetParams,
          rationale: `The assistant wants to run "${proposalTool}" - this requires your approval.`,
          ...(capability ? { capability } : {}),
        },
      });
      pending.push({
        type: "confirmation_required",
        data: { confirmation_id: confirmationId, kind: "tool_run", payload: { tool: proposalTool, params: targetParams } },
      });
      awaitingApproval = true;
      abort.abort();
      return { behavior: "deny", message: `"${bare}" requires operator approval before it can run.`, interrupt: true };
    };

    const q = sdk.query({
      prompt: buildPrompt(input),
      options: {
        env: childEnv,
        model: this.opts.model,
        maxTurns,
        settingSources: [],
        permissionMode: "default",
        abortController: abort,
        mcpServers: { [MCP_SERVER_NAME]: server },
        allowedTools: [APS_TEST_RUN_MCP_NAME],
        canUseTool,
      },
    });

    let turnCount = 0;
    let cumulativeTokens = 0;
    for await (const raw of q) {
      const msg = raw as Record<string, unknown>;
      if (msg.type === "assistant") {
        turnCount += 1;
        const message = (msg.message ?? {}) as Record<string, unknown>;
        const u = (message.usage ?? {}) as Record<string, number>;
        cumulativeTokens += (u.input_tokens ?? 0) + (u.output_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0);
      }

      const events = mapSdkMessage(raw, toolUseNames).map(debarify);
      let sawTerminal = false;
      for (const ev of events) {
        yield ev;
        if (ev.type === "turn_complete" || ev.type === "error") sawTerminal = true;
      }
      if (sawTerminal) return;

      if (pending.length) {
        for (const ev of pending.splice(0)) yield ev;
      }
      if (awaitingApproval) {
        yield { type: "turn_complete", data: { stop_reason: "awaiting_approval" } };
        return;
      }
      if (turnCount > maxTurns || cumulativeTokens > maxTotalTokens) {
        abort.abort();
        yield { type: "turn_complete", data: { stop_reason: "cap_hit" } };
        return;
      }
    }

    // The SDK's iterable ended (aborted or exhausted) without our loop body ever
    // getting another pull to notice a pending approval - e.g. canUseTool denies+
    // aborts while the generator is resolving the NEXT message, and the generator then
    // simply completes with nothing left to yield. Never leave the NDJSON stream
    // unterminated (converse.ts: every stream is "ALWAYS terminated by turn_complete or
    // error") - flush anything queued, then guarantee a terminal event either way.
    if (pending.length) {
      for (const ev of pending.splice(0)) yield ev;
    }
    yield { type: "turn_complete", data: { stop_reason: awaitingApproval ? "awaiting_approval" : "error" } };
  }

  private async capabilityFor(tenantId: string, targetTool: string | undefined): Promise<string | undefined> {
    if (!targetTool) return undefined;
    try {
      const repo = await this.ports.tenantRepo.checkout(tenantId);
      return findTool(repo.dir, targetTool)?.capabilities?.[0];
    } catch {
      return undefined;
    }
  }

  private async executeProposal(
    input: ConverseTurnInput,
    proposal: ConfirmProposal,
  ): Promise<{ ok: boolean; summary: string; jobId?: string }> {
    const repo = await this.ports.tenantRepo.checkout(input.tenant_id);
    const pkg = findTool(repo.dir, proposal.tool);
    if (!pkg) {
      return { ok: false, summary: `no registered tool named "${proposal.tool}" was found in this tenant's registry.` };
    }
    const envelope = await this.ports.broker.runTool({
      tenantId: input.tenant_id,
      tool: pkg,
      params: proposal.params ?? {},
      dwg: input.drawing_id,
      apsLive: false,
    });
    const jobId = extractJobId(envelope);
    return { ok: envelope.ok, summary: summarizeEnvelope(envelope), ...(jobId ? { jobId } : {}) };
  }
}

/** Fold the turn engine's bounded prior-turn context + the new user text into ONE
 *  prompt string, same "flat string prompt" shape AgentSdkRunner.ts already uses. */
function buildPrompt(input: ConverseTurnInput): string {
  const history = input.messages.map((m) => `${m.role === "user" ? "User" : "Assistant"}: ${m.text}`).join("\n");
  const newTurn = input.text
    ? `User: ${input.text}`
    : "Continue from the result above and tell the user what happened.";
  return [history, newTurn].filter((s) => s.length > 0).join("\n");
}
