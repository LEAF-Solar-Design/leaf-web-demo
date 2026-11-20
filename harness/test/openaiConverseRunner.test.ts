import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import {
  converseProviderFromEnv, createOpenAiSdkFacade, OpenAiConverseRunner,
} from "../src/ports/impl/openaiConverseRunner.js";

const apiKey = "test-openai-key-not-real-0000000000";
const model = "gpt-5";
const call = { type: "function_call", name: "job_status", arguments: '{"job_id":"first"}', call_id: "call-1" };
const answer = { type: "message", content: [{ type: "output_text", text: "Job finished" }] };
function reply(...output: unknown[]) {
  return Response.json({ model, output, usage: { input_tokens: 3, output_tokens: 2 } });
}

function setup(...responses: Response[]) {
  const fetchImpl = vi.fn<typeof fetch>(async () => {
    const response = responses.shift();
    if (!response) throw new Error("Unexpected request");
    return response;
  });
  const sdk = createOpenAiSdkFacade({ apiKey, model, fetchImpl });
  const handler = vi.fn(async (_args: Record<string, unknown>) => ({ content: [{ type: "text", text: "done" }] }));
  const shape = { job_id: z.string() };
  const spine = sdk.createSdkMcpServer({ name: "spine", tools: [
    sdk.tool(call.name, "Read job", shape, handler),
    sdk.tool("invalid.name", "Invalid", shape, handler),
  ] });
  const canUseTool = vi.fn(async (_name: string, _args: Record<string, unknown>) =>
    ({ behavior: "allow" as const, updatedInput: { job_id: "updated" } }));
  const options = { systemPrompt: "Instructions", model: "ignored", mcpServers: { spine }, canUseTool };
  const body = (index: number) => JSON.parse(String(fetchImpl.mock.calls[index]?.[1]?.body));
  return { sdk, options, fetchImpl, handler, canUseTool, body };
}

async function collect<T>(stream: AsyncIterable<T>) {
  const events: T[] = [];
  for await (const event of stream) events.push(event);
  return events;
}

describe("converseProviderFromEnv", () => {
  it("defaults to Anthropic and accepts explicit provider and model settings", () => {
    for (const provider of [undefined, "", "anthropic"]) {
      expect(converseProviderFromEnv({ LEAF_CONVERSE_PROVIDER: provider })).toEqual({ provider: "anthropic" });
    }
    const openai = { LEAF_CONVERSE_PROVIDER: "openai", OPENAI_API_KEY: apiKey };
    expect(converseProviderFromEnv(openai)).toEqual({ provider: "openai", apiKey, model: "gpt-5" });
    expect(converseProviderFromEnv({ ...openai, LEAF_OPENAI_MODEL: "another-model" }))
      .toMatchObject({ model: "another-model" });
    expect(() => converseProviderFromEnv({ LEAF_CONVERSE_PROVIDER: "openai" }))
      .toThrow("LEAF_CONVERSE_PROVIDER=openai needs OPENAI_API_KEY");
    expect(() => converseProviderFromEnv({ LEAF_CONVERSE_PROVIDER: apiKey }))
      .toThrow(/^Invalid LEAF_CONVERSE_PROVIDER$/);
  });

  it("reads OPENAI_BASE_URL: unset keeps the default, set is trimmed, invalid fails closed", () => {
    const openai = { LEAF_CONVERSE_PROVIDER: "openai", OPENAI_API_KEY: apiKey };
    for (const unset of [undefined, "", "   "]) {
      expect(converseProviderFromEnv({ ...openai, OPENAI_BASE_URL: unset })).not.toHaveProperty("baseUrl");
    }
    expect(converseProviderFromEnv({ ...openai, OPENAI_BASE_URL: " https://llm.example.test/v1/ " }))
      .toEqual({ provider: "openai", apiKey, model: "gpt-5", baseUrl: "https://llm.example.test/v1" });
    expect(converseProviderFromEnv({ ...openai, OPENAI_BASE_URL: "https://llm.example.test/v1//" }))
      .toMatchObject({ baseUrl: "https://llm.example.test/v1" });
    for (const invalid of [
      "not a url", "http://llm.example.test/v1", "ftp://llm.example.test/v1",
      "https://user:pass@llm.example.test/v1", "https://llm.example.test/v1?x=1", "https://llm.example.test/v1#f",
    ]) {
      expect(() => converseProviderFromEnv({ ...openai, OPENAI_BASE_URL: invalid }))
        .toThrow(/^Invalid OPENAI_BASE_URL$/);
    }
    expect(converseProviderFromEnv({ OPENAI_BASE_URL: "not a url" })).toEqual({ provider: "anthropic" });
  });

  it("sends requests to the configured base URL", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async () => reply(answer));
    const sdk = createOpenAiSdkFacade({ apiKey, model, fetchImpl, baseUrl: "https://llm.example.test/v1" });
    await collect(sdk.query({ prompt: "Hi", options: {} }));
    expect(fetchImpl.mock.calls[0]?.[0]).toBe("https://llm.example.test/v1/responses");
  });
});

describe("OpenAI SDK facade", () => {
  it("replays tool calls and gated outputs, and reports per-call and total usage", async () => {
    const { sdk, options, fetchImpl, canUseTool, handler, body } = setup(reply(call), reply(answer));
    const events = await collect(sdk.query({ prompt: "Check job", options }));
    expect(fetchImpl).toHaveBeenCalledWith("https://api.openai.com/v1/responses", expect.objectContaining({
      method: "POST", signal: expect.any(AbortSignal),
      headers: expect.objectContaining({ Authorization: `Bearer ${apiKey}` }),
    }));
    expect(body(0)).toMatchObject({ store: false, instructions: "Instructions", model, tools: [{
      type: "function", name: call.name, description: "Read job", strict: false,
      parameters: { type: "object", properties: { job_id: { type: "string" } }, required: ["job_id"] },
    }] });
    expect(canUseTool).toHaveBeenCalledWith("mcp__spine__job_status", { job_id: "first" });
    expect(handler).toHaveBeenCalledWith({ job_id: "updated" });
    expect(body(1).input).toEqual([...body(0).input, call,
      { type: "function_call_output", call_id: call.call_id, output: "done" }]);
    expect(body(1)).not.toHaveProperty("previous_response_id");
    const usage = { input_tokens: 3, output_tokens: 2 };
    expect(events).toMatchObject([
      { type: "assistant", message: { model, usage } },
      { type: "stream_event", event: { type: "content_block_delta", delta: { type: "text_delta", text: "Job finished" } } },
      { type: "assistant", message: { model, usage } },
      { type: "result", subtype: "success", num_turns: 2, usage: { input_tokens: 6, output_tokens: 4 } },
    ]);
    expect(new Set(events.map(event => event.session_id)).size).toBe(1);
    expect(events[0].session_id).toMatch(/^openai-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  });

  it("returns denials and malformed arguments as tool outputs", async () => {
    const bad = { ...call, call_id: "bad", arguments: "{" };
    const { sdk, options, handler, body } = setup(reply(call, bad), reply(answer));
    const deny = vi.fn(async () => ({ behavior: "deny" as const, message: "Permission denied" }));
    await collect(sdk.query({ prompt: "Check job", options: { ...options, canUseTool: deny } }));
    expect(deny).toHaveBeenCalledTimes(1);
    expect(handler).not.toHaveBeenCalled();
    expect(body(1).input.slice(-2)).toEqual([
      { type: "function_call_output", call_id: call.call_id, output: "Permission denied" },
      { type: "function_call_output", call_id: "bad", output: "Invalid tool arguments" },
    ]);
  });

  it("stops at the model call limit", async () => {
    const { sdk, options, fetchImpl } = setup(reply(call));
    const events = await collect(sdk.query({ prompt: "Check", options: { ...options, maxTurns: 1 } }));
    expect(events.at(-1)).toMatchObject({ type: "result", subtype: "error_max_turns", num_turns: 1 });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("rejects resume before contacting the provider", async () => {
    const { sdk, fetchImpl } = setup();
    await expect(collect(sdk.query({ prompt: "Hi", options: { resume: "missing" } })))
      .rejects.toThrow("No conversation found with session ID: missing");
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("reports a 429 retry horizon before the terminal rate-limit message", async () => {
    const { sdk } = setup(new Response(null, { status: 429, headers: { "retry-after": "420" } }));
    expect(await collect(sdk.query({ prompt: "Hi", options: {} }))).toMatchObject([
      { type: "system", subtype: "api_retry", retry_delay_ms: 420000 },
      { type: "assistant", error: "rate_limit", message: {} },
      { type: "result", subtype: "error_during_execution" },
    ]);
  });

  it("keeps HTTP and transport errors free of credentials and body text", async () => {
    const { sdk, fetchImpl } = setup(new Response(apiKey, { status: 500 }));
    const run = () => collect(sdk.query({ prompt: "private request", options: {} }));
    await expect(run()).rejects.toThrow(/^OpenAI request failed \(500\)$/);
    fetchImpl.mockRejectedValueOnce(new Error(apiKey));
    await expect(run()).rejects.toThrow(/^OpenAI request failed \(network\)$/);
  });

  it("converts structured prompt blocks in order", async () => {
    const { sdk, body } = setup(reply(answer));
    async function* prompt() {
      yield { message: { content: [
        { type: "image" as const, source: { type: "base64" as const, media_type: "image/png", data: "YWJj" } },
        { type: "text" as const, text: "Inspect" },
      ] } };
    }
    await collect(sdk.query({ prompt: prompt(), options: {} }));
    expect(body(0).input).toEqual([{ role: "user", content: [
      { type: "input_image", image_url: "data:image/png;base64,YWJj" }, { type: "input_text", text: "Inspect" },
    ] }]);
  });
});

it("runs through the host gates and recovers a missing session with visible history", async () => {
  const { fetchImpl, body } = setup(reply(call), reply(answer));
  const runner = new OpenAiConverseRunner({
    apiKey, model, fetchImpl, grant: { kind: "oauth", oauthToken: "fake-tenant-grant" },
  });
  const execute = vi.fn(async () => ({ content: "done" }));
  const events = await collect(runner.run({
    systemPrompt: "System", userMessage: "Check job", resumeSdkSessionId: "old",
    resumeFallbackUserMessage: "History and current question",
    tools: { list: () => ["job_status"], execute },
    canUseTool: async (_name, args) => ({ behavior: "allow", updatedInput: args }),
  }));
  expect(execute).toHaveBeenCalledWith("job_status", { job_id: "first" });
  expect(body(0).input[0].content[0].text).toBe("History and current question");
  expect(events).toContainEqual({ type: "text_delta", text: "Job finished" });
  expect(events.at(-1)).toMatchObject({ type: "done", stopReason: "end_turn", sdkSessionReset: true });
  for (const [, init] of fetchImpl.mock.calls) expect(String(init?.body)).not.toContain(apiKey);
});
