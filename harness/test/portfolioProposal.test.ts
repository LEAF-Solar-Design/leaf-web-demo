import { afterEach, describe, expect, it, vi } from "vitest";
import type { GrantLease, GrantSettlement, OAuthGrantProvider } from "../src/ports/index.js";
import { PortfolioProposalError, PortfolioProposalService } from "../src/agent/portfolioProposal.js";
import type { PortfolioProposalFailure, PortfolioProposalOptions, PortfolioProposalRequest } from "../src/agent/portfolioProposal.js";

const secret = "tenant-grant-12345678901234567890";
const exceptionSentinel = "exception-sentinel-do-not-log";
const lease: GrantLease = { grant: { kind: "oauth", oauthToken: secret }, account_id: "account", lease_id: "lease" };
function request(): PortfolioProposalRequest {
  return { tenant_id: "tenant", source: { project_id: "project", revision: 7, digest64hex: "a".repeat(64) },
    instruction: "Make the title concise", fields: [{ id: "title", value: "My supplied title", max_length: 100 }] };
}
function assistant(text: string) {
  return { type: "assistant", message: { content: [{ type: "text", text }] } };
}
function result(extra: Record<string, unknown> = {}) {
  return { type: "result", subtype: "success", is_error: false, ...extra };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function setup(events: unknown[] = [assistant('{"edits":[{"id":"title","value":"Concise"}]}'), result()], overrides: Partial<PortfolioProposalOptions> = {}) {
  const getGrant = vi.fn(async () => lease.grant);
  const acquireGrant = vi.fn(async () => lease);
  const settleGrant = vi.fn(async (_tenant: string, _id: string, _outcome: GrantSettlement) => {});
  const oauth: OAuthGrantProvider = { getGrant, acquireGrant, settleGrant };
  const query = vi.fn((_args: { prompt: string; options: Record<string, unknown> }) => (async function* () {
    for (const event of events) yield event;
  })());
  const sdkImport = vi.fn(async () => ({ query }));
  const logger = { warn: vi.fn((_record: PortfolioProposalFailure) => {}) };
  const service = new PortfolioProposalService({ oauth, sdkImport, logger, ...overrides });
  return { service, oauth, query, sdkImport, getGrant, acquireGrant, settleGrant, logger };
}
function expectFailure(logger: ReturnType<typeof setup>["logger"], stage: PortfolioProposalFailure["stage"], code: PortfolioProposalFailure["code"]) {
  // Compare the complete argument list as well as rendered output: no hidden error argument.
  expect(logger.warn.mock.calls).toEqual([[{
    event: "portfolio_proposal_failure",
    operation_id: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/),
    stage, code,
  }]]);
  const record = logger.warn.mock.calls[0][0];
  for (const sentinel of [secret, exceptionSentinel, request().instruction, "tenant", "project", "account", "configured-test-model"]) {
    expect(Object.values(record)).not.toContain(sentinel);
    expect(JSON.stringify(logger.warn.mock.calls)).not.toContain(sentinel);
  }
  return record;
}
afterEach(() => { vi.useRealTimers(); vi.unstubAllEnvs(); });

describe("source-bound portfolio proposals", () => {
  it("binds exact source, preserves HTML as data and reports observed usage once", async () => {
    const text = '{"edits":[{"id":"title","value":"<b>Concise</b>"}]}';
    const f = setup([
      { type: "stream_event", event: { delta: { text } } }, assistant(text),
      result({ result: text, usage: { input_tokens: 12, output_tokens: 8, cache_creation_input_tokens: 2, cache_read_input_tokens: 3 } }),
    ], { model: "configured-test-model" });
    const input = request();
    const output = await f.service.propose(input);
    expect(output).toEqual({ source: input.source, model: "configured-test-model", edits: [{ id: "title", value: "<b>Concise</b>" }],
      usage: { input_tokens: 12, output_tokens: 8, cache_creation_tokens: 2, cache_read_tokens: 3, cost_tokens: 22 } });
    expect(output.source).not.toBe(input.source);
    expect(f.getGrant).not.toHaveBeenCalled();
    expect(f.acquireGrant).toHaveBeenCalledExactlyOnceWith("tenant");
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: { cost_tokens: 22 }, stop_reason: "end_turn" });
    expect((f.query.mock.calls[0][0].options.abortController as AbortController).signal.aborted).toBe(true);
    expect(f.logger.warn).not.toHaveBeenCalled();
  });

  it("returns real no-change and leaves unknown usage absent", async () => {
    const f = setup([assistant('{"edits":[]}'), result()]);
    f.logger.warn.mockImplementation(() => { throw new Error("logger failed"); });
    expect(await f.service.propose(request())).toMatchObject({ edits: [], usage: {} });
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "end_turn" });
    expect(f.logger.warn).not.toHaveBeenCalled();
  });

  it("snapshots the trusted source and catalog before awaiting the lease", async () => {
    const late = deferred<GrantLease>();
    const f = setup();
    f.acquireGrant.mockReturnValueOnce(late.promise);
    const input = request();
    const pending = f.service.propose(input);
    input.source.revision = 99;
    input.fields[0].max_length = 1;
    input.instruction = "changed after start";
    late.resolve(lease);
    expect((await pending).source.revision).toBe(7);
    expect(f.query.mock.calls[0][0].prompt).not.toContain("changed after start");
  });

  it("keeps partial usage without fabricating cache or cost observations", async () => {
    const f = setup([assistant('{"edits":[]}'), result({ usage: { input_tokens: 0, output_tokens: 5 } })]);
    expect((await f.service.propose(request())).usage).toEqual({ input_tokens: 0, output_tokens: 5 });
    expect(f.settleGrant.mock.calls[0][2].usage).toEqual({});
  });

  it("preserves assistant-only observed usage on a failed SDK result", async () => {
    const f = setup([{ type: "assistant", message: { content: [], usage: {
      input_tokens: 2, output_tokens: 3, cache_creation_input_tokens: 0,
    } } }, result({ subtype: "error_during_execution", is_error: true })]);
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "sdk_failure" });
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: { cost_tokens: 5 }, stop_reason: "error" });
    expectFailure(f.logger, "sdk_query", "result_failed");
  });

  it.each([
    (r: PortfolioProposalRequest) => ({ ...r, extra: true }),
    (r: PortfolioProposalRequest) => ({ ...r, tenant_id: "" }),
    (r: PortfolioProposalRequest) => ({ ...r, tenant_id: "t".repeat(129) }),
    (r: PortfolioProposalRequest) => ({ ...r, source: { ...r.source, revision: 0 } }),
    (r: PortfolioProposalRequest) => ({ ...r, source: { ...r.source, revision: 1.5 } }),
    (r: PortfolioProposalRequest) => ({ ...r, source: { ...r.source, digest64hex: "not-a-digest" } }),
    (r: PortfolioProposalRequest) => ({ ...r, source: { ...r.source, extra: true } }),
    (r: PortfolioProposalRequest) => ({ ...r, instruction: "x".repeat(4001) }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [r.fields[0], r.fields[0]] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], id: "__proto__" }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], id: "constructor" }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], id: "prototype" }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], id: "x".repeat(129) }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], max_length: 0 }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], max_length: 8001 }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: [{ ...r.fields[0], value: "x".repeat(101) }] }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: Array.from({ length: 1025 }, (_, i) => ({ id: String(i), value: "", max_length: 1 })) }),
    (r: PortfolioProposalRequest) => ({ ...r, fields: Array.from({ length: 40 }, (_, i) => ({ id: String(i), value: "é".repeat(8000), max_length: 8000 })) }),
  ])("rejects invalid closed request before grant resolution (%#)", async mutate => {
    const f = setup();
    await expect(f.service.propose(mutate(request()) as PortfolioProposalRequest)).rejects.toMatchObject({ code: "invalid_request" });
    expect(f.acquireGrant).not.toHaveBeenCalled();
    expect(f.sdkImport).not.toHaveBeenCalled();
    expect(f.logger.warn).not.toHaveBeenCalled();
  });

  it.each([
    '{"edits":[{"id":"unknown","value":"x"}]}',
    '{"edits":[{"id":"title","value":"x"},{"id":"title","value":"y"}]}',
    JSON.stringify({ edits: [{ id: "title", value: "x".repeat(101) }] }),
    '{"edits":[{"id":"__proto__","value":"x"}]}',
    '{"edits":[{"id":"title","value":5}]}',
    '{"edits":[{"id":"title","value":"x","extra":true}]}',
    '{"edits":[],"source":{}}', '{"edits":[],"__proto__":{}}',
    '```json\n{"edits":[]}\n```', 'Here: {"edits":[]}', '{"edits":[]}{}', '{', '[]',
    JSON.stringify({ edits: Array.from({ length: 33 }, (_, i) => ({ id: String(i), value: "x" })) }),
    " ".repeat(65537),
    "é".repeat(32769),
  ])("rejects malformed or unbound edits and settles once (%#)", async text => {
    const f = setup([assistant(text), result()]);
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "invalid_output" });
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expect(f.settleGrant.mock.calls[0][2].stop_reason).toBe("error");
    expectFailure(f.logger, "sdk_query", "invalid_output");
  });

  it.each([
    { events: [], code: "missing_result" },
    { events: [assistant('{"edits":[]}')], code: "missing_result" },
    { events: [assistant('{"edits":[]}'), result({ subtype: "error_max_turns" })], code: "turn_limit" },
    { events: [assistant('{"edits":[]}'), result({ is_error: true })], code: "result_failed" },
    { events: [result({ result: '{"edits":[]}' })], code: "invalid_output" },
    { events: [{ ...assistant('{"edits":[]}'), error: "authentication_failed" }, result()], code: "authentication_failed" },
    { events: [{ type: "assistant", message: { content: [{ type: "text", text: '{"edits":[]}' }], stop_reason: "max_tokens" } }, result()], code: "output_token_limit" },
    { events: [result({ subtype: "error_during_execution", is_error: true })], code: "result_failed" },
    { events: [result({ subtype: exceptionSentinel })], code: "result_failed" },
    { events: [result({ subtype: undefined })], code: "result_shape_invalid" },
    { events: [result({ subtype: { body: exceptionSentinel } })], code: "result_shape_invalid" },
    { events: [result({ is_error: undefined })], code: "result_shape_invalid" },
    { events: [result({ is_error: exceptionSentinel })], code: "result_shape_invalid" },
    { events: [assistant('{"edits":[]}'), result(), assistant(exceptionSentinel)], code: "event_after_result" },
    { events: [assistant('{"edits":[]}'), result(), result()], code: "event_after_result" },
  ] satisfies Array<{ events: unknown[]; code: PortfolioProposalFailure["code"] }>)("requires successful SDK completion and assistant text (%#)", async ({ events, code }) => {
    const f = setup(events);
    await expect(f.service.propose(request())).rejects.toBeInstanceOf(PortfolioProposalError);
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "error" });
    expectFailure(f.logger, "sdk_query", code);
  });

  it.each([
    { error: "rate_limit", code: "rate_limit" },
    { error: "authentication_failed", code: "authentication_failed" },
    { error: "oauth_org_not_allowed", code: "oauth_org_not_allowed" },
    { error: "billing_error", code: "billing_error" },
    { error: exceptionSentinel, code: "assistant_error" },
    { error: "RATE_LIMIT", code: "assistant_error" },
    { error: "rate_limit ", code: "assistant_error" },
    { error: null, code: "assistant_error" },
    { error: false, code: "assistant_error" },
    { error: { code: "rate_limit", body: `${exceptionSentinel} ${secret}` }, code: "assistant_error" },
  ] satisfies Array<{ error: unknown; code: PortfolioProposalFailure["code"] }>)("allowlists only exact assistant error literals (%#)", async ({ error, code }) => {
    const f = setup([{ ...assistant(exceptionSentinel), error }, result()]);
    const input = request(), original = structuredClone(input);
    await expect(f.service.propose(input)).rejects.toMatchObject({
      code: code === "rate_limit" ? "rate_limit" : "sdk_failure",
      message: `Portfolio proposal failed: ${code === "rate_limit" ? "rate_limit" : "sdk_failure"}`,
    });
    expectFailure(f.logger, "sdk_query", code);
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", {
      usage: {}, stop_reason: code === "rate_limit" ? "llm_quota_exhausted" : "error",
    });
    expect(f.query).toHaveBeenCalledTimes(1);
    expect(input).toEqual(original);
  });

  it("settles a confirmed rate limit once with measured usage and no retry", async () => {
    const f = setup([{ type: "assistant", error: "rate_limit", message: { content: [], usage: {
      input_tokens: 2, output_tokens: 3, cache_creation_input_tokens: 0, cache_read_input_tokens: 7,
    } } }]);
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "rate_limit" });
    expect(f.acquireGrant).toHaveBeenCalledExactlyOnceWith("tenant");
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", {
      usage: { cost_tokens: 5 }, stop_reason: "llm_quota_exhausted",
    });
    expect(f.query).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "sdk_query", "rate_limit");
  });

  it("does not inspect or coerce arbitrary assistant error payloads", async () => {
    const inspect = vi.fn(() => { throw new Error(exceptionSentinel); });
    const error = { body: `${exceptionSentinel} ${secret}`, get message() { return inspect(); }, toString: inspect };
    const f = setup([{ ...assistant(exceptionSentinel), error }]);
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "sdk_failure" });
    expect(inspect).not.toHaveBeenCalled();
    expectFailure(f.logger, "sdk_query", "assistant_error");
  });

  it("contains SDK capabilities and injects only a scrubbed child environment", async () => {
    vi.stubEnv("LEAF_INTENT_MODEL", "claude-haiku-5-5");
    vi.stubEnv("ANTHROPIC_API_KEY", "ambient-key");
    vi.stubEnv("CLAUDE_CODE_OAUTH_TOKEN", "ambient-oauth");
    const f = setup();
    await f.service.propose(request());
    const { options, prompt } = f.query.mock.calls[0][0];
    expect(options).toMatchObject({ model: "claude-haiku-5-5", maxTurns: 1, tools: [], mcpServers: {}, strictMcpConfig: true,
      settingSources: [], permissionMode: "default", settings: { disableSkillShellExecution: true, disableAllHooks: true } });
    expect(options.env).toMatchObject({ CLAUDE_CODE_OAUTH_TOKEN: secret });
    expect((options.env as NodeJS.ProcessEnv).ANTHROPIC_API_KEY).toBeUndefined();
    expect(process.env.ANTHROPIC_API_KEY).toBe("ambient-key");
    expect(process.env.CLAUDE_CODE_OAUTH_TOKEN).toBe("ambient-oauth");
    expect(prompt).toContain("Never invent biography, work history or testimonials");
    expect(prompt).not.toContain("project_id");
  });

  it("strips actual grants from prompt strings and rejects their return", async () => {
    const f = setup([assistant(JSON.stringify({ edits: [{ id: "title", value: secret }] })), result()]);
    const input = request();
    input.instruction = `Preserve ${secret}`;
    input.fields[0].value = secret;
    await expect(f.service.propose(input)).rejects.toMatchObject({ code: "invalid_output" });
    expect(f.query.mock.calls[0][0].prompt).not.toContain(secret);
    expect(f.query.mock.calls[0][0].prompt).toContain("[REDACTED]");
    expect(JSON.stringify(f.settleGrant.mock.calls)).not.toContain(secret);
    expectFailure(f.logger, "sdk_query", "invalid_output");
  });

  it("rejects an escaped actual grant in parsed output", async () => {
    const escaped = secret.split("").map(c => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`).join("");
    const f = setup([assistant(`{"edits":[{"id":"title","value":"${escaped}"}]}`), result()]);
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "invalid_output" });
    expectFailure(f.logger, "sdk_query", "invalid_output");
  });

  it("strips JSON-escaped API grants before encoding prompt data", async () => {
    const apiKey = 'api-key-12345678901234567890"\\tail';
    const f = setup([assistant('{"edits":[]}'), result()]);
    f.acquireGrant.mockResolvedValueOnce({ ...lease, grant: { kind: "api_key", apiKey } });
    const input = request();
    input.instruction = `Preserve ${apiKey}`;
    input.fields[0].value = apiKey;
    await f.service.propose(input);
    const args = f.query.mock.calls[0][0];
    const data = JSON.parse(args.prompt.split("\n").at(-1)!);
    expect(data.instruction).toBe("Preserve [REDACTED]");
    expect(data.fields[0].value).toBe("[REDACTED]");
    expect((args.options.env as NodeJS.ProcessEnv).ANTHROPIC_API_KEY).toBe(apiKey);
    expect((args.options.env as NodeJS.ProcessEnv).CLAUDE_CODE_OAUTH_TOKEN).toBeUndefined();
  });

  it("requires lease support and sanitizes unavailable grants", async () => {
    const f = setup();
    const unleased = new PortfolioProposalService({ oauth: { getGrant: f.getGrant }, logger: f.logger });
    await expect(unleased.propose(request())).rejects.toMatchObject({ code: "grant_unavailable" });
    expect(f.getGrant).not.toHaveBeenCalled();
    const missing = expectFailure(f.logger, "lease_support", "lease_methods_missing");
    f.logger.warn.mockClear();
    f.acquireGrant.mockRejectedValueOnce(new Error(`${exceptionSentinel} ${secret}`));
    await expect(f.service.propose(request())).rejects.toThrow("Portfolio proposal failed: grant_unavailable");
    expect(f.settleGrant).not.toHaveBeenCalled();
    const rejected = expectFailure(f.logger, "acquisition", "acquisition_rejected");
    expect(rejected.operation_id).not.toBe(missing.operation_id);
    f.logger.warn.mockClear();
    f.acquireGrant.mockImplementationOnce(() => { throw new Error(exceptionSentinel); });
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "grant_unavailable" });
    expect(expectFailure(f.logger, "acquisition", "acquisition_rejected").operation_id).not.toBe(rejected.operation_id);
    expect(f.settleGrant).not.toHaveBeenCalled();
  });

  it("refuses unredactable credentials and settles their lease", async () => {
    const f = setup();
    f.acquireGrant.mockResolvedValueOnce({ ...lease, grant: { kind: "api_key", apiKey: "short" } });
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "grant_unavailable" });
    expect(f.query).not.toHaveBeenCalled();
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "grant", "grant_unusable");
  });

  it("fails sanitized SDK errors with exactly one settlement", async () => {
    const f = setup([], { sdkImport: async () => { throw new Error(`${exceptionSentinel} ${secret}`); } });
    await expect(f.service.propose(request())).rejects.toThrow("Portfolio proposal failed: sdk_failure");
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "sdk_import", "sdk_import_failed");
  });

  it("classifies a thrown SDK query without logging its arguments or exception", async () => {
    const f = setup([], { sdkImport: async () => ({ query: () => { throw new Error(`rate_limit 429 ${exceptionSentinel} ${secret}`); } }) });
    const input = request();
    input.instruction = `${input.instruction} ${exceptionSentinel} ${secret}`;
    await expect(f.service.propose(input)).rejects.toMatchObject({ code: "sdk_failure" });
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "error" });
    expectFailure(f.logger, "sdk_query", "sdk_exception_before_event");
  });

  it.each([
    { events: [] }, { events: [null] }, { events: [{ type: "system" }] },
    { events: [assistant('{"edits":[]}')] }, { events: [assistant('{"edits":[]}'), result()] },
  ])("classifies iterator exceptions by event observation without inspecting them (%#)", async ({ events }) => {
    const inspect = vi.fn(() => `${exceptionSentinel} ${secret}`);
    const error = { get message() { return inspect(); }, get stack() { return inspect(); }, get stderr() { return inspect(); } };
    const f = setup([], { sdkImport: async () => ({ query: () => (async function* () {
      for (const event of events) yield event;
      throw error;
    })() }) });
    await expect(f.service.propose(request())).rejects.toThrow("Portfolio proposal failed: sdk_failure");
    expect(inspect).not.toHaveBeenCalled();
    expectFailure(f.logger, "sdk_query", events.length === 0 ? "sdk_exception_before_event" : "sdk_exception_after_event");
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "error" });
  });

  it("preserves the original error and settlement when the warning logger throws", async () => {
    const original = new PortfolioProposalError("sdk_failure");
    const f = setup([], { sdkImport: async () => { throw original; } });
    f.logger.warn.mockImplementationOnce(() => { throw new Error("logger failed"); });
    await expect(f.service.propose(request())).rejects.toBe(original);
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "error" });
    expectFailure(f.logger, "sdk_import", "sdk_import_failed");
  });

  it("never reports success or retries when settlement fails", async () => {
    const f = setup();
    f.settleGrant.mockRejectedValueOnce(new Error(`${exceptionSentinel} ${secret}`));
    await expect(f.service.propose(request())).rejects.toThrow("Portfolio proposal failed: settlement_failure");
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "settlement", "settlement_failed");

    // Cleanup settlement still takes precedence over an earlier SDK failure.
    f.logger.warn.mockClear();
    f.query.mockImplementationOnce(() => (async function* () { throw new Error(exceptionSentinel); })());
    f.settleGrant.mockRejectedValueOnce(new Error(secret));
    await expect(f.service.propose(request())).rejects.toMatchObject({ code: "settlement_failure" });
    expect(f.settleGrant).toHaveBeenCalledTimes(2);
    expectFailure(f.logger, "settlement", "settlement_failed");
  });

  it("times out a late import without ever starting query", async () => {
    vi.useFakeTimers();
    const late = deferred<unknown>();
    const f = setup([], { timeoutMs: 20, sdkImport: () => late.promise });
    const outcome = expect(f.service.propose(request())).rejects.toMatchObject({ code: "timeout", processExitConfirmed: false });
    await vi.advanceTimersByTimeAsync(20);
    await outcome;
    late.resolve({ query: f.query });
    await Promise.resolve();
    expect(f.query).not.toHaveBeenCalled();
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", { usage: {}, stop_reason: "timeout" });
    expect(vi.getTimerCount()).toBe(0);
    expectFailure(f.logger, "sdk_import", "timeout");
  });

  it("checks caller abort before acquiring", async () => {
    const f = setup();
    const caller = new AbortController();
    caller.abort();
    await expect(f.service.propose(request(), caller.signal)).rejects.toMatchObject({ code: "aborted" });
    expect(f.acquireGrant).not.toHaveBeenCalled();
    expectFailure(f.logger, "lease_support", "aborted");
  });

  it("settles a grant that arrives after caller cancellation without importing", async () => {
    const late = deferred<GrantLease>();
    const f = setup();
    f.acquireGrant.mockReturnValueOnce(late.promise);
    const caller = new AbortController();
    const outcome = expect(f.service.propose(request(), caller.signal)).rejects.toMatchObject({ code: "aborted" });
    caller.abort();
    await outcome;
    expect(f.settleGrant).not.toHaveBeenCalled();
    late.resolve(lease);
    await Promise.resolve();
    await Promise.resolve();
    expect(f.sdkImport).not.toHaveBeenCalled();
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "acquisition", "aborted");
  });

  it("settles a grant arriving after timeout and handles late import rejection", async () => {
    vi.useFakeTimers();
    const grant = deferred<GrantLease>();
    const f = setup([], { timeoutMs: 20 });
    f.acquireGrant.mockReturnValueOnce(grant.promise);
    const outcome = expect(f.service.propose(request())).rejects.toMatchObject({ code: "timeout" });
    await vi.advanceTimersByTimeAsync(20);
    await outcome;
    grant.resolve(lease);
    await vi.advanceTimersByTimeAsync(0);
    expect(f.sdkImport).not.toHaveBeenCalled();
    expect(f.settleGrant).toHaveBeenCalledTimes(1);

    const lateImport = deferred<unknown>();
    const second = setup([], { timeoutMs: 20, sdkImport: () => lateImport.promise });
    const secondOutcome = expect(second.service.propose(request())).rejects.toMatchObject({ code: "timeout" });
    await vi.advanceTimersByTimeAsync(20);
    await secondOutcome;
    lateImport.reject(new Error(secret));
    await vi.advanceTimersByTimeAsync(0);
    expect(second.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "acquisition", "timeout");
    expectFailure(second.logger, "sdk_import", "timeout");
  });

  it("aborts a cooperative SDK before settling its lease", async () => {
    const started = deferred<AbortController>();
    let sawAbort = false;
    const f = setup([], { sdkImport: async () => ({
      query: (args: { options: Record<string, unknown> }) => (async function* () {
        const control = args.options.abortController as AbortController;
        const stopped = new Promise<void>(resolve => control.signal.addEventListener("abort", () => {
          sawAbort = true;
          resolve();
        }, { once: true }));
        started.resolve(control);
        await stopped;
        yield await Promise.reject(new Error("SDK aborted"));
      })(),
    }) });
    const caller = new AbortController();
    const outcome = expect(f.service.propose(request(), caller.signal)).rejects.toMatchObject({ code: "aborted" });
    await started.promise;
    f.logger.warn.mockImplementationOnce(() => { throw new Error("logger failed"); });
    f.settleGrant.mockImplementationOnce(async () => { expect(sawAbort).toBe(true); });
    caller.abort();
    await outcome;
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "sdk_query", "aborted");
  });

  it.each(["timeout", "aborted"] as const)("bounds an ignoring SDK, aborts before settlement and handles late rejection: %s", async code => {
    vi.useFakeTimers();
    const late = deferred<void>();
    const started = deferred<AbortController>();
    const caller = new AbortController();
    const f = setup([], { timeoutMs: 20, sdkImport: async () => ({
      query: (args: { options: Record<string, unknown> }) => (async function* () {
        started.resolve(args.options.abortController as AbortController);
        await late.promise;
        yield assistant('{"edits":[]}');
        yield result();
      })(),
    }) });
    const outcome = expect(f.service.propose(request(), caller.signal)).rejects.toMatchObject({ code });
    const control = await started.promise;
    f.settleGrant.mockImplementationOnce(async () => { expect(control.signal.aborted).toBe(true); });
    if (code === "aborted") caller.abort();
    else await vi.advanceTimersByTimeAsync(20);
    await outcome;
    late.reject(new Error(secret));
    await vi.advanceTimersByTimeAsync(0);
    expect(f.settleGrant).toHaveBeenCalledExactlyOnceWith("tenant", "lease", {
      usage: {}, stop_reason: code === "timeout" ? "timeout" : "error",
    });
    expect(vi.getTimerCount()).toBe(0);
    expectFailure(f.logger, "sdk_query", code);
  });

  it("bounds settlement too and never retries a hanging settlement", async () => {
    vi.useFakeTimers();
    const late = deferred<void>();
    const f = setup(undefined, { timeoutMs: 20 });
    f.settleGrant.mockReturnValueOnce(late.promise);
    const outcome = expect(f.service.propose(request())).rejects.toMatchObject({ code: "timeout" });
    await vi.advanceTimersByTimeAsync(20);
    await outcome;
    late.reject(new Error(secret));
    await vi.advanceTimersByTimeAsync(0);
    expect(f.settleGrant).toHaveBeenCalledTimes(1);
    expectFailure(f.logger, "settlement", "timeout");
  });
});
