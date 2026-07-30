/**
 * ConverseSdkRunner unit tests — the SDK is MOCKED AT THE MODULE BOUNDARY (the
 * runner's injectable sdkImport/zodImport seams stand in for the dynamic import
 * of @anthropic-ai/claude-agent-sdk). NO live Anthropic calls, NO network.
 *
 * Proves the B-1/B3 runner contract:
 *   - env scrubbing: the child env carries EXACTLY the tenant grant, ambient
 *     Anthropic identities are stripped (buildScrubbedEnv discipline);
 *   - options wiring: resume, model, includePartialMessages (true streaming),
 *     no bare allowedTools auto-approval, in-process MCP server;
 *   - tool bridging: MCP handlers delegate to the loop's ToolExecutor (the loop
 *     owns semantics; the runner only transports), isError passes through;
 *   - canUseTool bridging: the SDK's 3-arg hook delegates to the loop's hook;
 *   - wall-clock timeout: AbortController fires -> stop_reason "timeout";
 *   - 429 classification: horizon > 300s -> llm_quota_exhausted, else (or
 *     unknown) -> llm_rate_limited, horizon read from rate_limit_event/api_retry;
 *   - no shared mutable telemetry: two interleaved run()s never mix usage.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ConverseSdkRunner,
  QUOTA_HORIZON_S,
  classifyRateLimit,
} from "../src/ports/impl/converseSdkRunner.js";
import type { ConverseSdkRunnerOptions } from "../src/ports/impl/converseSdkRunner.js";
import type {
  ConverseRunInput,
  ConverseRunnerEvent,
  SpineToolResult,
  ToolExecutor,
} from "../src/ports/index.js";
import { SPINE_TOOL_NAMES } from "../src/ports/index.js";

// --------------------------------------------------------------------------- //
// The mock SDK module (mirrors the REAL shapes read from sdk.d.ts 0.3.214).
// --------------------------------------------------------------------------- //

type AnyRec = Record<string, unknown>;

interface CapturedQuery {
  prompt: string;
  options: AnyRec;
}

interface CapturedTool {
  name: string;
  description: string;
  schema: AnyRec;
  handler: (args: AnyRec, extra: unknown) => Promise<{
    content: Array<{ type: "text"; text: string }>;
    isError?: boolean;
  }>;
}

/** Script entries: either a message object, or "HANG" (wait for abort). */
type ScriptEntry = AnyRec | Error | "HANG";

function makeMockSdk(script: ScriptEntry[] | ScriptEntry[][]) {
  const queries: CapturedQuery[] = [];
  const tools: CapturedTool[] = [];
  const servers: AnyRec[] = [];

  const sdkModule = {
    tool(name: string, description: string, schema: AnyRec, handler: CapturedTool["handler"]) {
      const def: CapturedTool = { name, description, schema, handler };
      tools.push(def);
      return def;
    },
    createSdkMcpServer(opts: AnyRec) {
      servers.push(opts);
      return { __mockServer: true, name: opts.name };
    },
    query({ prompt, options }: { prompt: string; options: AnyRec }) {
      queries.push({ prompt, options });
      const queryIndex = queries.length - 1;
      const entries = Array.isArray(script[0])
        ? (script as ScriptEntry[][])[queryIndex] ?? []
        : (script as ScriptEntry[]);
      async function* gen(): AsyncGenerator<unknown> {
        for (const entry of entries) {
          if (entry === "HANG") {
            const abort = options.abortController as AbortController;
            await new Promise<void>((resolve) => {
              if (abort.signal.aborted) return resolve();
              abort.signal.addEventListener("abort", () => resolve(), { once: true });
            });
            // A real aborted SDK stream throws out of the iterator.
            throw new Error("aborted by AbortController");
          }
          if (entry instanceof Error) throw entry;
          yield entry;
        }
      }
      return gen();
    },
  };

  // A zod stand-in: every factory returns a chainable token with .optional().
  const zt = (): AnyRec => ({ optional: zt });
  const zodModule = {
    z: { string: zt, number: zt, unknown: zt, boolean: zt, enum: zt, record: zt, array: zt, object: zt },
  };

  return {
    queries,
    tools,
    servers,
    sdkImport: async () => sdkModule,
    zodImport: async () => zodModule,
  };
}

// --- message builders (real SDK message shapes) ----------------------------- //

const SDK_SESSION = "sdk-session-0001";

function streamDelta(text: string): AnyRec {
  return {
    type: "stream_event",
    event: { type: "content_block_delta", delta: { type: "text_delta", text } },
    session_id: SDK_SESSION,
  };
}

function assistant(err?: string): AnyRec {
  return {
    type: "assistant",
    message: {
      model: "claude-sonnet-5",
      usage: {
        input_tokens: 10,
        output_tokens: 5,
        cache_creation_input_tokens: 2,
        cache_read_input_tokens: 100,
      },
    },
    session_id: SDK_SESSION,
    ...(err ? { error: err } : {}),
  };
}

function resultSuccess(): AnyRec {
  return {
    type: "result",
    subtype: "success",
    num_turns: 2,
    total_cost_usd: 0.0123,
    usage: {
      input_tokens: 20,
      output_tokens: 9,
      cache_creation_input_tokens: 4,
      cache_read_input_tokens: 200,
    },
    modelUsage: { "claude-sonnet-5": {} },
    session_id: SDK_SESSION,
  };
}

function rateLimitEvent(resetsAtEpochS: number): AnyRec {
  return {
    type: "rate_limit_event",
    rate_limit_info: { status: "rejected", resetsAt: resetsAtEpochS },
    session_id: SDK_SESSION,
  };
}

/** SDKAPIRetryMessage (sdk.d.ts:2807-2817) — the OTHER horizon source. */
function apiRetry(retryDelayMs: number): AnyRec {
  return {
    type: "system",
    subtype: "api_retry",
    retry_delay_ms: retryDelayMs,
    error_status: 429,
    error: "rate_limit_error",
    session_id: SDK_SESSION,
  };
}

// --------------------------------------------------------------------------- //
// Harness helpers
// --------------------------------------------------------------------------- //

function makeExecutor(
  impl?: (tool: string, args: AnyRec) => Promise<SpineToolResult>,
): ToolExecutor & { calls: Array<{ tool: string; args: AnyRec }> } {
  const calls: Array<{ tool: string; args: AnyRec }> = [];
  return {
    calls,
    list: () => SPINE_TOOL_NAMES,
    async execute(tool, args) {
      calls.push({ tool, args });
      if (impl) return impl(tool, args);
      return { content: JSON.stringify({ ok: true, tool }) };
    },
  };
}

function makeInput(overrides: Partial<ConverseRunInput> = {}): ConverseRunInput {
  return {
    systemPrompt: "SPINE SYSTEM PROMPT",
    userMessage: "=== USER MESSAGE ===\nhello",
    tools: makeExecutor(),
    canUseTool: async (_tool, input) => ({ behavior: "allow", updatedInput: input }),
    ...overrides,
  };
}

async function collect(
  runner: ConverseSdkRunner,
  input: ConverseRunInput,
): Promise<ConverseRunnerEvent[]> {
  const events: ConverseRunnerEvent[] = [];
  for await (const ev of runner.run(input)) events.push(ev);
  return events;
}

function doneOf(events: ConverseRunnerEvent[]) {
  const done = events.find((e) => e.type === "done");
  if (!done || done.type !== "done") throw new Error("no done event");
  return done;
}

function runnerWith(
  mock: ReturnType<typeof makeMockSdk>,
  opts: Partial<ConverseSdkRunnerOptions> = {},
): ConverseSdkRunner {
  return new ConverseSdkRunner({
    grant: { kind: "oauth", oauthToken: "FAKE-OAUTH-not-real-abc" },
    model: "claude-sonnet-5",
    sdkImport: mock.sdkImport,
    zodImport: mock.zodImport,
    ...opts,
  });
}

afterEach(() => {
  vi.unstubAllEnvs();
});

// --------------------------------------------------------------------------- //
// Env scrubbing
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — env scrubbing", () => {
  it("oauth grant: child env carries CLAUDE_CODE_OAUTH_TOKEN only; ambient creds stripped", async () => {
    vi.stubEnv("ANTHROPIC_API_KEY", "ambient-api-key-MUST-NOT-LEAK");
    vi.stubEnv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-oauth-MUST-NOT-LEAK");
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput());

    const env = mock.queries[0]!.options.env as Record<string, string | undefined>;
    expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBe("FAKE-OAUTH-not-real-abc");
    expect(env.ANTHROPIC_API_KEY).toBeUndefined();
  });

  it("api_key grant: child env carries ANTHROPIC_API_KEY only", async () => {
    vi.stubEnv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-oauth-MUST-NOT-LEAK");
    const mock = makeMockSdk([resultSuccess()]);
    const runner = runnerWith(mock, { grant: { kind: "api_key", apiKey: "sk-ant-api-FAKE" } });
    await collect(runner, makeInput());

    const env = mock.queries[0]!.options.env as Record<string, string | undefined>;
    expect(env.ANTHROPIC_API_KEY).toBe("sk-ant-api-FAKE");
    expect(env.CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
  });
});

// --------------------------------------------------------------------------- //
// Credential redaction on a throw (sol-critic PR #123 round 6, non-blocking 4:
// this security boundary was correct by inspection but had no direct test).
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — a throw never carries the grant out", () => {
  // Clears the app's 24-char floor, but is deliberately invisible to TOKENISH
  // (not sk-ant-*, under 40 chars) so this proves VALUE-based redaction rather
  // than passing on the pattern.
  const GRANT = "linked-grant-value-98765!";

  it("strips the held grant from an SDK error that quotes it", async () => {
    const mock = makeMockSdk([]);
    // Node/undici header validation is the realistic case: the message embeds
    // the offending header VALUE, which here is the credential. Override only
    // `query` so the rest of the module keeps its real shape.
    const base = await mock.sdkImport();
    mock.sdkImport = async () =>
      Object.assign({}, base, {
        query: () => {
          throw new Error(`Invalid header value: ${GRANT}`);
        },
      });

    const runner = runnerWith(mock, {
      grant: { kind: "oauth", oauthToken: GRANT },
    });

    const events = await collect(runner, makeInput()).catch((e: Error) => e);
    const serialized = JSON.stringify(events) + String(events);

    expect(serialized).not.toContain(GRANT);
    expect(serialized).toContain("[REDACTED]");
  });

  it("strips the grant from a throw that ESCAPES runInner (the public wrapper)", async () => {
    // The test above is caught by runInner's own stream-fault handler, so it
    // does not exercise run()'s outer try (sol-critic PR #123 round 7 caught
    // that gap). sdkImport/zodImport are awaited BEFORE that inner try, so a
    // rejection there escapes runInner and must be scrubbed by the public
    // wrapper — the last boundary before ConverseLoop persists the error.
    const mock = makeMockSdk([]);
    mock.sdkImport = async () => {
      throw new Error(`module load failed for token ${GRANT}`);
    };

    const runner = runnerWith(mock, { grant: { kind: "oauth", oauthToken: GRANT } });

    const outcome = await collect(runner, makeInput()).catch((e: Error) => e);
    expect(outcome).toBeInstanceOf(Error);
    const err = outcome as Error;

    expect(err.message).not.toContain(GRANT);
    expect(err.message).toContain("[REDACTED]");
    // The original stack can quote the value too, so it must be replaced.
    expect(err.stack ?? "").not.toContain(GRANT);
  });
});

// --------------------------------------------------------------------------- //
// Options wiring (resume / streaming / tool allowlist / model)
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — SDK options wiring", () => {
  it("hardens the session and does NOT mount a skill bundle yet", async () => {
    // Skills are deliberately unmounted on the live path (see the comment in
    // converseSdkRunner): handing the SDK a plugin DIRECTORY kept reaching
    // execution outside canUseTool and the gate, and the correct fix is
    // digest-verified mounting, shipping as its own chip.
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput());
    const options = mock.queries[0]!.options;
    expect("skills" in options).toBe(false);
    expect("plugins" in options).toBe(false);
    // The hardening stays regardless: SETTINGS layer, not top-level — the
    // SDK's arg builder reads options.settings and never a top-level flag, so
    // the broken form silently does nothing.
    expect(options.settings).toMatchObject({
      disableSkillShellExecution: true,
      disableAllHooks: true,
    });
    expect("disableSkillShellExecution" in options).toBe(false);
    expect("disableAllHooks" in options).toBe(false);
  });

  it("passes resume when resumeSdkSessionId is present, omits it when absent", async () => {
    const withResume = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(withResume), makeInput({ resumeSdkSessionId: "prior-session-42" }));
    expect(withResume.queries[0]!.options.resume).toBe("prior-session-42");

    const fresh = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(fresh), makeInput());
    expect("resume" in fresh.queries[0]!.options).toBe(false);
  });

  it("restarts once without resume when the SDK transcript is missing", async () => {
    const missing = new Error(
      "Claude Code returned an error result: No conversation found with session ID: 8b1f10e2-38f2-4293-a035-23a0a2cc5e96",
    );
    const mock = makeMockSdk([[missing], [streamDelta("Recovered"), resultSuccess()]]);
    const events = await collect(
      runnerWith(mock),
      makeInput({
        resumeSdkSessionId: "8b1f10e2-38f2-4293-a035-23a0a2cc5e96",
        resumeFallbackUserMessage: "bounded prior history plus current turn",
      }),
    );

    expect(mock.queries).toHaveLength(2);
    expect(mock.queries[0]!.options.resume).toBe(
      "8b1f10e2-38f2-4293-a035-23a0a2cc5e96",
    );
    expect("resume" in mock.queries[1]!.options).toBe(false);
    expect(mock.queries[1]!.prompt).toBe("bounded prior history plus current turn");
    expect(events).toContainEqual({ type: "text_delta", text: "Recovered" });
    expect(doneOf(events)).toMatchObject({
      stopReason: "end_turn",
      sdkSessionId: SDK_SESSION,
      sdkSessionReset: true,
    });
  });

  it("does not retry unrelated errors or a missing-session fault after output", async () => {
    const unrelated = makeMockSdk([[new Error("network failed")], [resultSuccess()]]);
    const unrelatedEvents = await collect(
      runnerWith(unrelated),
      makeInput({ resumeSdkSessionId: "prior-session" }),
    );
    expect(unrelated.queries).toHaveLength(1);
    expect(doneOf(unrelatedEvents).error?.message).toBe("network failed");

    const afterOutput = makeMockSdk([
      [streamDelta("partial"), new Error("No conversation found with session ID: stale-id")],
      [resultSuccess()],
    ]);
    const afterOutputEvents = await collect(
      runnerWith(afterOutput),
      makeInput({ resumeSdkSessionId: "stale-id" }),
    );
    expect(afterOutput.queries).toHaveLength(1);
    expect(doneOf(afterOutputEvents).stopReason).toBe("error");
  });

  it("wires true streaming + the fixed six-tool MCP surface (no built-ins)", async () => {
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput());

    const opts = mock.queries[0]!.options;
    expect(opts.includePartialMessages).toBe(true);
    expect(opts.tools).toEqual([]); // built-in tools disabled
    expect(opts.allowedTools).toBeUndefined();
    expect(opts.systemPrompt).toBe("SPINE SYSTEM PROMPT");
    expect((opts.mcpServers as AnyRec).spine).toBeDefined();
    expect(mock.servers[0]!.name).toBe("spine");
    expect(mock.tools.map((t) => t.name)).toEqual([...SPINE_TOOL_NAMES]);
  });

  it("model precedence: input.model > constructor model", async () => {
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput({ model: "claude-haiku-4-5" }));
    expect(mock.queries[0]!.options.model).toBe("claude-haiku-4-5");
  });

  it("defaults the model to claude-sonnet-5 when neither option nor env is set", async () => {
    const prior = process.env.LEAF_SPINE_MODEL;
    delete process.env.LEAF_SPINE_MODEL;
    try {
      const mock = makeMockSdk([resultSuccess()]);
      const runner = new ConverseSdkRunner({
        grant: { kind: "oauth", oauthToken: "FAKE" },
        sdkImport: mock.sdkImport,
        zodImport: mock.zodImport,
      });
      await collect(runner, makeInput());
      expect(mock.queries[0]!.options.model).toBe("claude-sonnet-5");
    } finally {
      if (prior !== undefined) process.env.LEAF_SPINE_MODEL = prior;
    }
  });
});

// --------------------------------------------------------------------------- //
// Tool + canUseTool bridging
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — bridging to the loop", () => {
  it("MCP tool handlers delegate to the ToolExecutor and adapt the result", async () => {
    const executor = makeExecutor(async (tool) =>
      tool === "job_status"
        ? { content: "boom", isError: true }
        : { content: JSON.stringify({ matches: [] }) },
    );
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput({ tools: executor }));

    const search = mock.tools.find((t) => t.name === "catalog_search")!;
    const okRes = await search.handler({ query: "layers" }, undefined);
    expect(executor.calls).toContainEqual({ tool: "catalog_search", args: { query: "layers" } });
    expect(okRes).toEqual({ content: [{ type: "text", text: JSON.stringify({ matches: [] }) }] });

    const job = mock.tools.find((t) => t.name === "job_status")!;
    const errRes = await job.handler({ job_id: "j1" }, undefined);
    expect(errRes.isError).toBe(true);
    expect(errRes.content[0]!.text).toBe("boom");

    const author = mock.tools.find((t) => t.name === "author_tool")!;
    await author.handler(
      { description: "arrange panels as text", mode: "build", confirmation_id: "cid-author-1" },
      undefined,
    );
    expect(executor.calls).toContainEqual({
      tool: "author_tool",
      args: {
        description: "arrange panels as text",
        mode: "build",
        confirmation_id: "cid-author-1",
      },
    });
  });

  it("the SDK canUseTool hook delegates to the loop's CanUseTool (gate chain upstream)", async () => {
    const seen: Array<{ tool: string; input: AnyRec }> = [];
    const canUseTool: ConverseRunInput["canUseTool"] = async (tool, input) => {
      seen.push({ tool, input });
      return tool === "Skill" || tool.includes("run_capability")
        ? { behavior: "deny", message: "not now" }
        : { behavior: "allow", updatedInput: input };
    };
    const mock = makeMockSdk([resultSuccess()]);
    await collect(runnerWith(mock), makeInput({ canUseTool }));

    const hook = mock.queries[0]!.options.canUseTool as (
      t: string,
      i: AnyRec,
      extra?: unknown,
    ) => Promise<AnyRec>;
    const allow = await hook("mcp__spine__drawing_state", { what: "summary" }, { signal: null });
    expect(allow).toEqual({ behavior: "allow", updatedInput: { what: "summary" } });
    const deny = await hook("mcp__spine__run_capability", { tool: "add-panel" });
    expect(deny).toEqual({ behavior: "deny", message: "not now" });
    const skill = await hook("Skill", { skill: "drawing-help" });
    expect(skill).toEqual({ behavior: "deny", message: "not now" });
    expect(seen.map((s) => s.tool)).toEqual([
      "mcp__spine__drawing_state",
      "mcp__spine__run_capability",
      "Skill",
    ]);
  });
});

// --------------------------------------------------------------------------- //
// Streaming, usage, terminal states
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — events", () => {
  it("relays stream_event text deltas exactly once (assistant messages are metering-only)", async () => {
    const mock = makeMockSdk([streamDelta("Hel"), streamDelta("lo"), assistant(), resultSuccess()]);
    const events = await collect(runnerWith(mock), makeInput());
    const texts = events.filter((e) => e.type === "text_delta").map((e) => (e as { text: string }).text);
    expect(texts).toEqual(["Hel", "lo"]);
  });

  it("emits usage (result totals authoritative; cost excludes cache_read) and a clean done", async () => {
    const mock = makeMockSdk([assistant(), resultSuccess()]);
    const events = await collect(runnerWith(mock), makeInput());

    const usage = events.find((e) => e.type === "usage");
    expect(usage && usage.type === "usage" ? usage.usage : null).toMatchObject({
      turns: 2,
      input_tokens: 20,
      output_tokens: 9,
      cache_creation_tokens: 4,
      cache_read_tokens: 200,
      cost_tokens: 33, // 20 + 9 + 4 (cache_read excluded)
      total_cost_usd: 0.0123,
    });

    const done = doneOf(events);
    expect(done.stopReason).toBe("end_turn");
    expect(done.sdkSessionId).toBe(SDK_SESSION);
    expect(done.error).toBeUndefined();
  });

  it("maps result subtype error_max_turns to cap_hit", async () => {
    const mock = makeMockSdk([assistant(), { ...resultSuccess(), subtype: "error_max_turns" }]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("cap_hit");
    expect(done.error?.error_code).toBe("cap_hit");
  });

  it("maps a terminal auth failure to stop_reason error (not retryable)", async () => {
    const mock = makeMockSdk([assistant("authentication_failed")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("error");
    expect(done.error).toMatchObject({ error_code: "authentication_failed", retryable: false });
  });
});

// --------------------------------------------------------------------------- //
// Wall-clock timeout (B3)
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — wall-clock timeout", () => {
  it("aborts a hung turn via AbortController and reports stop_reason timeout", async () => {
    const mock = makeMockSdk([streamDelta("thinking"), "HANG"]);
    const runner = runnerWith(mock, { turnTimeoutS: 0.05 });
    const events = await collect(runner, makeInput());
    const done = doneOf(events);
    expect(done.stopReason).toBe("timeout");
    expect(done.error).toMatchObject({ error_code: "timeout", retryable: true });
  });
});

// --------------------------------------------------------------------------- //
// 429 classification (B3): horizon > 300s => quota exhausted, else rate limited
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — rate-limit classification", () => {
  it("rate_limit with a LONG horizon (> 300s) => llm_quota_exhausted", async () => {
    const resetsAt = Date.now() / 1000 + 600;
    const mock = makeMockSdk([rateLimitEvent(resetsAt), assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_quota_exhausted");
    expect(done.error).toMatchObject({ error_code: "llm_quota_exhausted", retryable: true });
  });

  it("rate_limit with a SHORT horizon (<= 300s) => llm_rate_limited", async () => {
    const resetsAt = Date.now() / 1000 + 30;
    const mock = makeMockSdk([rateLimitEvent(resetsAt), assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_rate_limited");
    expect(done.error).toMatchObject({ error_code: "llm_rate_limited", retryable: true });
  });

  it("rate_limit with NO horizon information => llm_rate_limited (transient default)", async () => {
    const mock = makeMockSdk([assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_rate_limited");
  });

  it("horizon arriving ONLY via api_retry with a LONG delay (> 300s) => llm_quota_exhausted", async () => {
    const mock = makeMockSdk([apiRetry(600_000), assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_quota_exhausted");
    expect(done.error).toMatchObject({ error_code: "llm_quota_exhausted", retryable: true });
  });

  it("horizon arriving ONLY via api_retry with a SHORT delay (<= 300s) => llm_rate_limited", async () => {
    const mock = makeMockSdk([apiRetry(30_000), assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_rate_limited");
  });

  it("normalizes a millisecond resetsAt (epoch-ms vs epoch-s ambiguity)", async () => {
    const resetsAtMs = Date.now() + 600_000; // +600s, in ms
    const mock = makeMockSdk([rateLimitEvent(resetsAtMs), assistant("rate_limit")]);
    const done = doneOf(await collect(runnerWith(mock), makeInput()));
    expect(done.stopReason).toBe("llm_quota_exhausted");
  });

  it("classifyRateLimit splits exactly at the 300s horizon", () => {
    expect(QUOTA_HORIZON_S).toBe(300);
    expect(classifyRateLimit(301)).toBe("llm_quota_exhausted");
    expect(classifyRateLimit(300)).toBe("llm_rate_limited");
    expect(classifyRateLimit(5)).toBe("llm_rate_limited");
    expect(classifyRateLimit(null)).toBe("llm_rate_limited");
  });
});

// --------------------------------------------------------------------------- //
// No shared mutable telemetry (the agentSdkRunner usageLog bleed must not recur)
// --------------------------------------------------------------------------- //

describe("ConverseSdkRunner — instance isolation", () => {
  it("two interleaved runs on separate instances never mix usage or session ids", async () => {
    const mockA = makeMockSdk([assistant(), resultSuccess()]);
    const mockB = makeMockSdk([
      assistant(),
      {
        ...resultSuccess(),
        num_turns: 7,
        total_cost_usd: 9.99,
        session_id: "sdk-session-OTHER",
        usage: {
          input_tokens: 1000,
          output_tokens: 500,
          cache_creation_input_tokens: 0,
          cache_read_input_tokens: 0,
        },
      },
    ]);
    const [a, b] = await Promise.all([
      collect(runnerWith(mockA), makeInput()),
      collect(runnerWith(mockB), makeInput()),
    ]);
    const usageA = a.find((e) => e.type === "usage");
    const usageB = b.find((e) => e.type === "usage");
    expect(usageA && usageA.type === "usage" ? usageA.usage.cost_tokens : -1).toBe(33);
    expect(usageB && usageB.type === "usage" ? usageB.usage.cost_tokens : -1).toBe(1500);
    expect(doneOf(a).sdkSessionId).toBe(SDK_SESSION);
    expect(doneOf(b).sdkSessionId).toBe("sdk-session-OTHER");
  });
});
