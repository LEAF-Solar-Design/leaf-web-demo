/**
 * Hermetic unit tests for the ConverseRunner turn-mapping layer (lane H2). Fixture
 * messages ONLY - shaped exactly like the real Agent SDK's SDKAssistantMessage /
 * SDKUserMessage / SDKResultMessage (verified against
 * node_modules/@anthropic-ai/claude-agent-sdk/sdk.d.ts, see the header comment in
 * agentSdkTurnRunner.ts). NO SDK import, NO network, NO Anthropic auth: `mapSdkMessage`
 * is a pure function of its arguments and is exercised directly.
 */

import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  AgentSdkTurnRunner,
  buildPrompt,
  chunkText,
  mapSdkMessage,
  resolveWrapperTarget,
  stopReasonFromResult,
  summarizeArgs,
  summarizeToolResultContent,
} from "../src/ports/impl/agentSdkTurnRunner.js";
import type { BrokerApsClient, HarnessTurnEvent, OAuthGrantProvider, TenantRepoProvider } from "../src/ports/index.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

// --------------------------------------------------------------------------- //
// Fixture builders - minimal, type-loose (mapSdkMessage takes `unknown`), shaped
// exactly like the real SDK's discriminated union members.
// --------------------------------------------------------------------------- //

function assistantText(text: string, opts: { error?: string } = {}): unknown {
  return {
    type: "assistant",
    uuid: "00000000-0000-0000-0000-000000000001",
    session_id: "sess-1",
    parent_tool_use_id: null,
    ...(opts.error ? { error: opts.error } : {}),
    message: { content: opts.error ? [] : [{ type: "text", text }] },
  };
}

function assistantToolUse(id: string, name: string, input: unknown): unknown {
  return {
    type: "assistant",
    uuid: "00000000-0000-0000-0000-000000000002",
    session_id: "sess-1",
    parent_tool_use_id: null,
    message: { content: [{ type: "tool_use", id, name, input }] },
  };
}

function userToolResult(toolUseId: string, content: unknown, isError = false): unknown {
  return {
    type: "user",
    session_id: "sess-1",
    parent_tool_use_id: null,
    message: { content: [{ type: "tool_result", tool_use_id: toolUseId, content, is_error: isError }] },
  };
}

function resultMessage(over: Partial<Record<string, unknown>> = {}): unknown {
  return {
    type: "result",
    subtype: "success",
    duration_ms: 100,
    duration_api_ms: 90,
    is_error: false,
    num_turns: 1,
    result: "done",
    stop_reason: null,
    total_cost_usd: 0.0123,
    usage: { input_tokens: 10, output_tokens: 5, cache_creation_input_tokens: 2, cache_read_input_tokens: 100 },
    modelUsage: {},
    permission_denials: [],
    uuid: "00000000-0000-0000-0000-000000000003",
    session_id: "sess-1",
    ...over,
  };
}

// --------------------------------------------------------------------------- //
// chunkText
// --------------------------------------------------------------------------- //

describe("chunkText", () => {
  it("returns no chunks for empty text", () => {
    expect(chunkText("")).toEqual([]);
  });

  it("returns one chunk for short text", () => {
    expect(chunkText("hello world")).toEqual(["hello world"]);
  });

  it("round-trips: concatenated chunks always reconstruct the original text exactly", () => {
    const samples = [
      "a",
      "The quick brown fox jumps over the lazy dog, repeatedly, until the sentence is long enough to wrap across several synthetic chunks.",
      "supercalifragilisticexpialidocious".repeat(3), // no spaces at all -> hard-cut path
      "  leading and trailing whitespace   ",
      "line one\nline two\nline three with more words than fit in one chunk of forty eight characters",
    ];
    for (const text of samples) {
      expect(chunkText(text).join("")).toBe(text);
    }
  });

  it("splits long text into multiple chunks", () => {
    const text = "word ".repeat(30).trim();
    const chunks = chunkText(text);
    expect(chunks.length).toBeGreaterThan(1);
    expect(chunks.join("")).toBe(text);
  });
});

describe("buildPrompt", () => {
  it("keeps text-only prompts byte-identical and emits image blocks before text", async () => {
    const input = {
      tenant_id: "acme", session_id: "s", turn_id: "t", drawing_id: "d",
      messages: [{ role: "assistant" as const, text: "Earlier answer" }], text: "inspect this",
    };
    expect(buildPrompt(input)).toBe("Assistant: Earlier answer\nUser: inspect this");
    const prompt = buildPrompt({ ...input, images: [{ media_type: "image/png", data: "iVBORw0KGgo=" }] });
    expect(typeof prompt).not.toBe("string");
    const messages = [];
    for await (const message of prompt as AsyncIterable<unknown>) messages.push(message);
    expect(messages).toEqual([{
      type: "user", parent_tool_use_id: null,
      message: { role: "user", content: [
        { type: "image", source: { type: "base64", media_type: "image/png", data: "iVBORw0KGgo=" } },
        { type: "text", text: "Assistant: Earlier answer\nUser: inspect this" },
      ] },
    }]);
  });
});

// --------------------------------------------------------------------------- //
// summarizeArgs / summarizeToolResultContent
// --------------------------------------------------------------------------- //

describe("summarizeArgs", () => {
  it("JSON-stringifies small input verbatim", () => {
    expect(summarizeArgs({ a: 1, b: "two" })).toBe(JSON.stringify({ a: 1, b: "two" }));
  });

  it("truncates large input with a marker", () => {
    const big = { blob: "x".repeat(1000) };
    const out = summarizeArgs(big);
    expect(out.length).toBeLessThan(400);
    expect(out.endsWith("… (truncated)")).toBe(true);
  });
});

describe("summarizeToolResultContent", () => {
  it("passes a plain string through", () => {
    expect(summarizeToolResultContent("all good")).toBe("all good");
  });

  it("joins text blocks from an array", () => {
    expect(summarizeToolResultContent([{ type: "text", text: "line1" }, { type: "text", text: "line2" }])).toBe(
      "line1\nline2",
    );
  });

  it("returns empty string for unrecognized content shapes", () => {
    expect(summarizeToolResultContent(42)).toBe("");
    expect(summarizeToolResultContent(undefined)).toBe("");
  });

  it("truncates long content", () => {
    const out = summarizeToolResultContent("y".repeat(2000));
    expect(out.length).toBeLessThan(600);
    expect(out.endsWith("… (truncated)")).toBe(true);
  });
});

// --------------------------------------------------------------------------- //
// stopReasonFromResult
// --------------------------------------------------------------------------- //

describe("stopReasonFromResult", () => {
  it("maps subtype success -> end_turn", () => {
    expect(stopReasonFromResult({ subtype: "success" })).toBe("end_turn");
  });

  it.each(["error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"])(
    "maps subtype %s -> cap_hit",
    (subtype) => {
      expect(stopReasonFromResult({ subtype })).toBe("cap_hit");
    },
  );

  it("maps error_during_execution with a rate-limit error -> llm_rate_limited", () => {
    expect(
      stopReasonFromResult({ subtype: "error_during_execution", errors: ["hit a rate limit, retry later"] }),
    ).toBe("llm_rate_limited");
  });

  it("maps error_during_execution with a 429 stop_reason -> llm_rate_limited", () => {
    expect(stopReasonFromResult({ subtype: "error_during_execution", stop_reason: "429 overloaded" })).toBe(
      "llm_rate_limited",
    );
  });

  it("maps error_during_execution with a quota/billing error -> llm_quota_exhausted", () => {
    expect(
      stopReasonFromResult({ subtype: "error_during_execution", errors: ["insufficient credit balance"] }),
    ).toBe("llm_quota_exhausted");
  });

  it("falls back to error for an unrecognized error_during_execution", () => {
    expect(stopReasonFromResult({ subtype: "error_during_execution", errors: ["something odd happened"] })).toBe(
      "error",
    );
  });
});

// --------------------------------------------------------------------------- //
// mapSdkMessage - assistant messages
// --------------------------------------------------------------------------- //

describe("mapSdkMessage: assistant text", () => {
  it("chunks a text block into ordered text_delta events that reconstruct the original", () => {
    const text = "Here is a fairly long assistant response that should span more than one synthetic chunk of text.";
    const events = mapSdkMessage(assistantText(text));
    expect(events.every((e) => e.type === "text_delta")).toBe(true);
    const joined = events.map((e) => (e.data as { text: string }).text).join("");
    expect(joined).toBe(text);
  });

  it("emits nothing for an empty text block", () => {
    expect(mapSdkMessage(assistantText(""))).toEqual([]);
  });
});

describe("mapSdkMessage: assistant tool_use", () => {
  it("emits tool_call with tool name and args_summary, and records the id->name mapping", () => {
    const toolUseNames = new Map<string, string>();
    const events = mapSdkMessage(assistantToolUse("tu_1", "mcp__converse__aps_test_run", { tool: "roof-area", params_json: "{}" }), toolUseNames);
    expect(events).toEqual([
      {
        type: "tool_call",
        data: {
          tool: "mcp__converse__aps_test_run",
          args_summary: JSON.stringify({ tool: "roof-area", params_json: "{}" }),
        },
      },
    ]);
    expect(toolUseNames.get("tu_1")).toBe("mcp__converse__aps_test_run");
  });

  it("truncates a large args_summary", () => {
    const events = mapSdkMessage(assistantToolUse("tu_2", "some_tool", { blob: "z".repeat(1000) }));
    const summary = (events[0].data as { args_summary: string }).args_summary;
    expect(summary.endsWith("… (truncated)")).toBe(true);
  });

  it("preserves block order across a mixed text + tool_use assistant message", () => {
    const msg = {
      type: "assistant",
      uuid: "u",
      session_id: "s",
      parent_tool_use_id: null,
      message: {
        content: [
          { type: "text", text: "Let me check that." },
          { type: "tool_use", id: "tu_3", name: "aps_test_run", input: { tool: "x" } },
        ],
      },
    };
    const events = mapSdkMessage(msg);
    expect(events[0].type).toBe("text_delta");
    expect(events[events.length - 1].type).toBe("tool_call");
  });
});

describe("mapSdkMessage: assistant errors", () => {
  it("maps a rate_limit error to turn_complete{llm_rate_limited} only", () => {
    expect(mapSdkMessage(assistantText("", { error: "rate_limit" }))).toEqual([
      { type: "turn_complete", data: { stop_reason: "llm_rate_limited" } },
    ]);
  });

  it("maps an overloaded error to turn_complete{llm_rate_limited}", () => {
    expect(mapSdkMessage(assistantText("", { error: "overloaded" }))).toEqual([
      { type: "turn_complete", data: { stop_reason: "llm_rate_limited" } },
    ]);
  });

  it("maps a billing_error to turn_complete{llm_quota_exhausted}", () => {
    expect(mapSdkMessage(assistantText("", { error: "billing_error" }))).toEqual([
      { type: "turn_complete", data: { stop_reason: "llm_quota_exhausted" } },
    ]);
  });

  it("maps an unrecognized fatal error to an error event followed by turn_complete{error}", () => {
    const events = mapSdkMessage(assistantText("", { error: "authentication_failed" }));
    expect(events).toEqual([
      { type: "error", data: { error: { error_code: "authentication_failed", message: "Agent SDK turn failed: authentication_failed" } } },
      { type: "turn_complete", data: { stop_reason: "error" } },
    ]);
  });
});

// --------------------------------------------------------------------------- //
// mapSdkMessage - user (tool_result) messages
// --------------------------------------------------------------------------- //

describe("mapSdkMessage: user tool_result", () => {
  it("correlates the tool name from a prior tool_use via the shared toolUseNames map", () => {
    const toolUseNames = new Map<string, string>();
    mapSdkMessage(assistantToolUse("tu_9", "aps_test_run", { tool: "roof-area" }), toolUseNames);
    const events = mapSdkMessage(userToolResult("tu_9", "ok: {\"area\":123}"), toolUseNames);
    expect(events).toEqual([{ type: "tool_result", data: { tool: "aps_test_run", ok: true, summary: 'ok: {"area":123}' } }]);
  });

  it("falls back to tool:'unknown' when the tool_use_id was never seen", () => {
    const events = mapSdkMessage(userToolResult("tu_missing", "some result"));
    expect(events).toEqual([{ type: "tool_result", data: { tool: "unknown", ok: true, summary: "some result" } }]);
  });

  it("maps is_error:true to ok:false", () => {
    const events = mapSdkMessage(userToolResult("tu_x", "boom", true));
    expect((events[0].data as { ok: boolean }).ok).toBe(false);
  });

  it("summarizes array-of-text-block content", () => {
    const events = mapSdkMessage(userToolResult("tu_y", [{ type: "text", text: "part1" }, { type: "text", text: "part2" }]));
    expect((events[0].data as { summary: string }).summary).toBe("part1\npart2");
  });

  it("emits nothing for a user message with no content array (plain string content)", () => {
    expect(mapSdkMessage({ type: "user", message: { content: "just a string" } })).toEqual([]);
  });

  it("emits nothing for a user message with non-tool_result content blocks", () => {
    expect(mapSdkMessage({ type: "user", message: { content: [{ type: "text", text: "hi" }] } })).toEqual([]);
  });
});

// --------------------------------------------------------------------------- //
// mapSdkMessage - result messages (turn_usage + turn_complete)
// --------------------------------------------------------------------------- //

describe("mapSdkMessage: result", () => {
  it("emits turn_usage with cost_tokens excluding cache_read, plus turn_complete{end_turn} on success", () => {
    const events = mapSdkMessage(resultMessage());
    expect(events).toEqual([
      { type: "turn_usage", data: { cost_tokens: 10 + 5 + 2, total_cost_usd: 0.0123 } },
      { type: "turn_complete", data: { stop_reason: "end_turn" } },
    ]);
  });

  it("omits total_cost_usd when it is not a number", () => {
    const events = mapSdkMessage(resultMessage({ total_cost_usd: undefined }));
    expect(events[0].data).toEqual({ cost_tokens: 17 });
  });

  it("maps a max-turns result to turn_complete{cap_hit}", () => {
    const events = mapSdkMessage(resultMessage({ subtype: "error_max_turns", is_error: true }));
    expect(events[1]).toEqual({ type: "turn_complete", data: { stop_reason: "cap_hit" } });
  });

  it("maps a rate-limited error_during_execution result to turn_complete{llm_rate_limited}", () => {
    const events = mapSdkMessage(
      resultMessage({ subtype: "error_during_execution", is_error: true, errors: ["429 rate limit exceeded"] }),
    );
    expect(events[1]).toEqual({ type: "turn_complete", data: { stop_reason: "llm_rate_limited" } });
  });
});

// --------------------------------------------------------------------------- //
// mapSdkMessage - unrecognized message types are safely ignored
// --------------------------------------------------------------------------- //

describe("mapSdkMessage: ignored message types", () => {
  it.each([
    { type: "system", subtype: "init" },
    { type: "tool_progress", tool_use_id: "tu_1", tool_name: "x" },
    { type: "stream_event", event: { type: "content_block_delta" } },
    { type: "auth_status", isAuthenticating: false, output: [] },
    null,
    undefined,
    "not an object",
    42,
  ])("returns [] for %j", (fixture) => {
    expect(mapSdkMessage(fixture)).toEqual([]);
  });
});

// --------------------------------------------------------------------------- //
// AgentSdkTurnRunner - construction-only smoke test (NEVER calls runTurn: doing so
// would dynamically import the real SDK and attempt a live call, which is exactly the
// network/credit-spending path this hermetic suite must not touch).
// --------------------------------------------------------------------------- //

describe("AgentSdkTurnRunner", () => {
  it("constructs against the real port interfaces and exposes an async-generator runTurn", () => {
    const oauth: OAuthGrantProvider = {
      async getGrant() {
        return { kind: "oauth", oauthToken: "sk-ant-oat01-unused" };
      },
    };
    const tenantRepo: TenantRepoProvider = {
      async checkout() {
        throw new Error("not used by this construction-only test");
      },
    };
    const broker: BrokerApsClient = {
      async runTool() {
        throw new Error("not used by this construction-only test");
      },
    };
    const runner = new AgentSdkTurnRunner({ oauth, tenantRepo, broker });
    expect(typeof runner.runTurn).toBe("function");
    // Calling it returns an async generator synchronously (no work happens until the
    // first next() is pulled) - confirms the shape without ever draining it.
    const iterable = runner.runTurn({
      tenant_id: "t1",
      session_id: "s1",
      turn_id: "turn1",
      drawing_id: "d1",
      messages: [],
      text: "hello",
    });
    expect(typeof (iterable as AsyncIterable<HarnessTurnEvent>)[Symbol.asyncIterator]).toBe("function");
  });
});

// --------------------------------------------------------------------------- //
// resolveWrapperTarget - FINDING 2 (resume executes the wrapper, not the target).
// `aps_test_run` is a WRAPPER tool; its own call args are {tool, params_json}. This
// pure helper is what `canUseTool`'s interception uses to persist the proposal, so
// asserting it directly is a hermetic, SDK-free way to lock in the fix without
// dynamically importing the real Agent SDK (see the untested-lane note above).
// --------------------------------------------------------------------------- //

describe("resolveWrapperTarget", () => {
  it("extracts the inner target tool name + parsed params from the aps_test_run wrapper's call args", () => {
    expect(
      resolveWrapperTarget("aps_test_run", { tool: "count-by-layer", params_json: JSON.stringify({ foo: 1 }) }),
    ).toEqual({ tool: "count-by-layer", params: { foo: 1 } });
  });

  it("defaults params to {} when params_json is absent", () => {
    expect(resolveWrapperTarget("aps_test_run", { tool: "count-by-layer" })).toEqual({
      tool: "count-by-layer",
      params: {},
    });
  });

  it("defaults params to {} when params_json is malformed JSON (never throws)", () => {
    expect(resolveWrapperTarget("aps_test_run", { tool: "count-by-layer", params_json: "not json" })).toEqual({
      tool: "count-by-layer",
      params: {},
    });
  });

  it("falls back to the wrapper's own bare name only when the wrapper's args carry no target tool", () => {
    expect(resolveWrapperTarget("aps_test_run", {})).toEqual({ tool: "aps_test_run", params: {} });
  });
});

// --------------------------------------------------------------------------- //
// AgentSdkTurnRunner.runConfirm - FINDING 2, resume side: given a proposal that
// (post-fix) already carries the TARGET tool (e.g. "count-by-layer", never
// "aps_test_run"), confirm-resume must dispatch broker.runTool with that target.
// Stays hermetic by pulling ONLY the first yielded event (the confirm branch's
// `tool_result`) and never continuing into `driveSession`, which dynamically
// imports the real Agent SDK.
// --------------------------------------------------------------------------- //

describe("AgentSdkTurnRunner.runConfirm: dispatches the TARGET tool, not the wrapper", () => {
  it("resume executes the proposal's target tool via broker.runTool", async () => {
    const oauth: OAuthGrantProvider = new FakeOAuthGrantProvider();
    const tenantRepo: TenantRepoProvider = new FakeTenantRepoProvider(FIXTURE);
    const broker = new FakeBrokerApsClient();
    const runner = new AgentSdkTurnRunner({ oauth, tenantRepo, broker });

    const iterator = runner
      .runTurn({
        tenant_id: "acme",
        session_id: "s1",
        turn_id: "turn1",
        drawing_id: "d1",
        messages: [],
        confirm: {
          confirmation_id: "conf-1",
          approved: true,
          // What a fixed `canUseTool` now persists: the inner target, never the
          // "aps_test_run" wrapper.
          proposal: { tool: "count-by-layer", params: {} },
        },
      })
      [Symbol.asyncIterator]();

    const first = await iterator.next();
    expect(first.done).toBe(false);
    expect(first.value).toMatchObject({ type: "tool_result", data: { tool: "count-by-layer", ok: true } });

    expect(broker.calls).toHaveLength(1);
    expect(broker.calls[0].tool.name).toBe("count-by-layer");
    expect(broker.calls[0].tool.name).not.toBe("aps_test_run");

    // Deliberately stop pulling here: the next event would fall through into
    // driveSession() (a fresh continuation session), which dynamically imports the
    // real Agent SDK - forbidden in this hermetic suite.
    await iterator.return?.(undefined);
  });

  it("looking up 'aps_test_run' itself (the pre-fix bug) would fail to find a registered tool", async () => {
    const oauth: OAuthGrantProvider = new FakeOAuthGrantProvider();
    const tenantRepo: TenantRepoProvider = new FakeTenantRepoProvider(FIXTURE);
    const broker = new FakeBrokerApsClient();
    const runner = new AgentSdkTurnRunner({ oauth, tenantRepo, broker });

    const iterator = runner
      .runTurn({
        tenant_id: "acme",
        session_id: "s1",
        turn_id: "turn1",
        drawing_id: "d1",
        messages: [],
        confirm: {
          confirmation_id: "conf-2",
          approved: true,
          proposal: { tool: "aps_test_run", params: {} },
        },
      })
      [Symbol.asyncIterator]();

    const first = await iterator.next();
    expect(first.value).toMatchObject({
      type: "tool_result",
      data: { tool: "aps_test_run", ok: false },
    });
    const summary = (first.value as HarnessTurnEvent).data.summary as string;
    expect(summary).toContain("no registered tool named");
    expect(broker.calls).toHaveLength(0);

    await iterator.return?.(undefined);
  });
});
