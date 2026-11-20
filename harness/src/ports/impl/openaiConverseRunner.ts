import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { ConverseRunInput, SpineConverseRunner } from "../index.js";
import { ConverseSdkRunner, type ConverseSdkRunnerOptions } from "./converseSdkRunner.js";

interface OpenAiOptions {
  apiKey: string;
  model: string;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
  /** Deadline for ONE model request, headers through the last streamed byte. Default 120 s. */
  requestTimeoutMs?: number;
}

/** Default per-request deadline; unchanged from the non-streaming runner. */
export const DEFAULT_OPENAI_REQUEST_TIMEOUT_MS = 120_000;
// Bounds on one streamed response. A server that exceeds them is treated as a failed request,
// so a misbehaving endpoint can never grow the harness heap without limit.
const MAX_SSE_EVENT_BYTES = 4 * 1024 * 1024;
const MAX_SSE_STREAM_BYTES = 32 * 1024 * 1024;
// Gateway timeouts before any response byte (Cloudflare 524, proxies 502/503/504) are retried
// once: nothing was yielded yet, and a self-hosted server keeps the prefix it already prefilled.
const RETRYABLE_GATEWAY_STATUSES = new Set([502, 503, 504, 524]);
const GATEWAY_RETRIES = 1;

// LEAF_OPENAI_TURN_TIMEOUT_S sets the OpenAI runner's turn deadline and, through it, the
// per-request deadline. Unset keeps both defaults exactly. Fails closed on anything but an
// integer from 10 to 1800 seconds.
export function openAiTurnTimeoutSFromEnv(raw: string | undefined): number | undefined {
  const value = raw?.trim();
  if (!value) return undefined;
  if (!/^[0-9]{1,4}$/.test(value)) throw new Error("Invalid LEAF_OPENAI_TURN_TIMEOUT_S");
  const seconds = Number(value);
  if (seconds < 10 || seconds > 1800) throw new Error("Invalid LEAF_OPENAI_TURN_TIMEOUT_S");
  return seconds;
}

// OPENAI_BASE_URL points the runner at any OpenAI Responses-compatible server (for example a
// self-hosted model behind a tunnel). Fails closed: only an absolute https URL with no
// credentials, query or fragment is accepted; trailing slashes are trimmed. Unset keeps api.openai.com.
function openAiBaseUrlFromEnv(raw: string | undefined): string | undefined {
  const value = raw?.trim();
  if (!value) return undefined;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("Invalid OPENAI_BASE_URL");
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) {
    throw new Error("Invalid OPENAI_BASE_URL");
  }
  return url.href.replace(/\/+$/, "");
}

export function converseProviderFromEnv(env: NodeJS.ProcessEnv):
  { provider: "anthropic" } |
  { provider: "openai"; apiKey: string; model: string; baseUrl?: string; turnTimeoutS?: number } {
  switch (env.LEAF_CONVERSE_PROVIDER) {
    case undefined:
    case "":
    case "anthropic":
      return { provider: "anthropic" };
    case "openai": {
      const apiKey = env.OPENAI_API_KEY?.trim();
      if (!apiKey) throw new Error("LEAF_CONVERSE_PROVIDER=openai needs OPENAI_API_KEY");
      const baseUrl = openAiBaseUrlFromEnv(env.OPENAI_BASE_URL);
      const turnTimeoutS = openAiTurnTimeoutSFromEnv(env.LEAF_OPENAI_TURN_TIMEOUT_S);
      return {
        provider: "openai", apiKey, model: env.LEAF_OPENAI_MODEL?.trim() || "gpt-5",
        ...(baseUrl ? { baseUrl } : {}),
        ...(turnTimeoutS !== undefined ? { turnTimeoutS } : {}),
      };
    }
    default:
      throw new Error("Invalid LEAF_CONVERSE_PROVIDER");
  }
}

type Args = Record<string, unknown>;
interface Tool {
  name: string;
  description: string;
  shape: z.ZodRawShape;
  handler(args: Args): Promise<{ content: { type: string; text?: string }[] }>;
}
interface Server {
  name: string;
  tools: Tool[];
}
interface QueryOptions {
  model?: string;
  systemPrompt?: string;
  mcpServers?: Record<string, Server>;
  canUseTool?: (name: string, args: Args) => Promise<
    { behavior: "allow"; updatedInput?: Args } | { behavior: "deny"; message: string }
  >;
  resume?: string;
  maxTurns?: number;
  abortController?: AbortController;
}
type PromptBlock = { type: "text"; text: string } |
  { type: "image"; source: { type: "base64"; media_type: string; data: string } };
type Prompt = string | AsyncIterable<{ message: { content: PromptBlock[] } }>;

async function promptItems(prompt: Prompt): Promise<Args[]> {
  if (typeof prompt === "string") return [{ role: "user", content: [{ type: "input_text", text: prompt }] }];
  const items: Args[] = [];
  for await (const { message } of prompt) {
    const content = message.content.map(block => block.type === "text"
      ? { type: "input_text", text: block.text }
      : { type: "input_image", image_url: `data:${block.source.media_type};base64,${block.source.data}` });
    items.push({ role: "user", content });
  }
  return items;
}

type FunctionCall = { type: "function_call"; name: string; arguments: string; call_id: string };
type MessageItem = { type: "message"; content: { type: string; text?: string }[] };
type Usage = { input_tokens?: number; output_tokens?: number };
interface ModelResponse {
  output: (FunctionCall | MessageItem | { type: "reasoning" })[];
  usage?: Usage;
  model?: string;
  status?: string;
}

type StreamEvent = { type?: unknown; delta?: unknown; response?: unknown };

// Reads one Responses API server-sent-event stream. Text deltas go to onText as they arrive;
// the terminal response.completed body is returned. Bounded: one event at most
// MAX_SSE_EVENT_BYTES, the whole stream at most MAX_SSE_STREAM_BYTES. Fails closed on a
// failed, incomplete, malformed or truncated stream. Errors never carry body text.
async function readResponseStream(body: ReadableStream<Uint8Array>,
  onText: (text: string) => void): Promise<ModelResponse> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let total = 0;
  const fail = (): never => {
    void reader.cancel().catch(() => {});
    throw new Error("OpenAI request failed (stream)");
  };
  const handle = (block: string): ModelResponse | undefined => {
    const data = block.split("\n").filter(line => line.startsWith("data:"))
      .map(line => line.slice(5).replace(/^ /, "")).join("\n");
    if (!data || data === "[DONE]") return undefined;
    let event: StreamEvent;
    try {
      event = JSON.parse(data) as StreamEvent;
    } catch {
      return fail();
    }
    if (event?.type === "response.output_text.delta") {
      if (typeof event.delta === "string" && event.delta) onText(event.delta);
      return undefined;
    }
    if (event?.type === "response.completed") {
      const response = event.response as ModelResponse | undefined;
      if (!response || typeof response !== "object") return fail();
      return response;
    }
    if (event?.type === "error" || event?.type === "response.failed" || event?.type === "response.incomplete") {
      return fail();
    }
    return undefined;
  };
  for (;;) {
    const { done, value } = await reader.read();
    if (value) {
      total += value.byteLength;
      if (total > MAX_SSE_STREAM_BYTES) fail();
      buffer += decoder.decode(value, { stream: true });
    }
    if (done) buffer += decoder.decode() + "\n\n";
    buffer = buffer.replace(/\r\n?/g, "\n");
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const completed = handle(block);
      if (completed) {
        void reader.cancel().catch(() => {});
        return completed;
      }
    }
    if (buffer.length > MAX_SSE_EVENT_BYTES) fail();
    if (done) return fail();
  }
}

async function functionOutput(call: FunctionCall, catalog: Map<string, { tool: Tool; qualified: string }>,
  canUseTool: QueryOptions["canUseTool"]): Promise<string> {
  const entry = catalog.get(call.name);
  if (!entry || !canUseTool) return "Tool unavailable";
  let args: Args;
  try {
    args = JSON.parse(call.arguments);
    if (!args || typeof args !== "object" || Array.isArray(args)) return "Invalid tool arguments";
  } catch {
    return "Invalid tool arguments";
  }
  const permission = await canUseTool(entry.qualified, args);
  if (permission.behavior !== "allow") return permission.message;
  const result = await entry.tool.handler(permission.updatedInput ?? args);
  return result.content.filter(block => block.type === "text").map(block => block.text).join("\n");
}

export function createOpenAiSdkFacade(opts: OpenAiOptions) {
  const endpoint = (opts.baseUrl ?? "https://api.openai.com/v1").replace(/\/$/, "") + "/responses";
  return {
    tool(name: string, description: string, shape: Tool["shape"], handler: Tool["handler"]): Tool {
      return { name, description, shape, handler };
    },
    createSdkMcpServer({ name, tools }: Server) {
      return { type: "sdk", name, tools };
    },
    async *query({ prompt, options }: { prompt: Prompt; options: QueryOptions }) {
      if (options.resume !== undefined) throw new Error("No conversation found with session ID: " + options.resume);
      const session_id = `openai-${randomUUID()}`;
      const catalog = new Map<string, { tool: Tool; qualified: string }>();
      for (const server of Object.values(options.mcpServers ?? {})) {
        for (const tool of server.tools) {
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(tool.name)) continue;
          if (catalog.has(tool.name)) throw new Error("Duplicate OpenAI function name");
          catalog.set(tool.name, { tool, qualified: `mcp__${server.name}__${tool.name}` });
        }
      }
      const tools = Array.from(catalog.values(), ({ tool }) => ({
        type: "function", name: tool.name, description: tool.description, strict: false,
        parameters: z.toJSONSchema(z.object(tool.shape), { io: "input" }),
      }));
      const input = await promptItems(prompt);
      const total: Usage = {};
      let turns = 0;
      const requestTimeoutMs = opts.requestTimeoutMs ?? DEFAULT_OPENAI_REQUEST_TIMEOUT_MS;
      const textEvent = (text: string) => ({ type: "stream_event", session_id,
        event: { type: "content_block_delta", delta: { type: "text_delta", text } } });
      while (turns < (options.maxTurns ?? 25)) {
        const deadline = AbortSignal.timeout(requestTimeoutMs);
        const signal = options.abortController
          ? AbortSignal.any([deadline, options.abortController.signal]) : deadline;
        let response: Response;
        for (let attempt = 0; ; attempt++) {
          try {
            response = await (opts.fetchImpl ?? fetch)(endpoint, {
              method: "POST", redirect: "manual", signal,
              headers: {
                Authorization: `Bearer ${opts.apiKey}`, "Content-Type": "application/json",
                Accept: "text/event-stream",
              },
              body: JSON.stringify({
                model: opts.model, instructions: options.systemPrompt, tools, input, store: false,
                include: ["reasoning.encrypted_content"], stream: true,
              }),
            });
          } catch {
            throw new Error("OpenAI request failed (network)");
          }
          if (attempt < GATEWAY_RETRIES && RETRYABLE_GATEWAY_STATUSES.has(response.status) && !signal.aborted) {
            void response.body?.cancel().catch(() => {});
            continue;
          }
          break;
        }
        turns++;
        if (response.status === 429) {
          const header = response.headers.get("retry-after")?.trim();
          const seconds = header ? Number(header) : NaN;
          const retry_delay_ms = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 60000;
          yield { type: "system", subtype: "api_retry", retry_delay_ms, session_id };
          yield { type: "assistant", error: "rate_limit", message: {}, session_id };
          yield { type: "result", subtype: "error_during_execution", session_id, num_turns: turns };
          return;
        }
        if (!response.ok) throw new Error(`OpenAI request failed (${response.status})`);
        let data: ModelResponse;
        let streamedText = false;
        const contentType = (response.headers.get("content-type") ?? "").toLowerCase();
        if (contentType.includes("text/event-stream") && response.body) {
          // The reader runs ahead and queues deltas; this generator relays them as they land, so
          // a proxy with an idle limit (Cloudflare's 100 s) stays open while the model generates.
          const pending: string[] = [];
          const reading: {
            wake?: () => void;
            outcome?: { ok: true; value: ModelResponse } | { ok: false; error: unknown };
          } = {};
          void readResponseStream(response.body, text => {
            pending.push(text);
            reading.wake?.();
          }).then(value => { reading.outcome = { ok: true, value }; },
            error => { reading.outcome = { ok: false, error }; })
            .finally(() => reading.wake?.());
          let outcome: { ok: true; value: ModelResponse } | { ok: false; error: unknown };
          for (;;) {
            while (pending.length) {
              streamedText = true;
              yield textEvent(pending.shift()!);
            }
            if (reading.outcome) {
              outcome = reading.outcome;
              break;
            }
            await new Promise<void>(resolve => { reading.wake = resolve; });
            reading.wake = undefined;
          }
          if (!outcome.ok) {
            const error = outcome.error;
            if (!signal.aborted && error instanceof Error && error.message === "OpenAI request failed (stream)") {
              throw error;
            }
            throw new Error("OpenAI request failed (network)");
          }
          data = outcome.value;
        } else {
          try {
            data = await response.json() as ModelResponse;
          } catch {
            throw new Error("OpenAI request failed (network)");
          }
        }
        if (!Array.isArray(data?.output) || (data.status && data.status !== "completed")) {
          throw new Error(`OpenAI request failed (${response.status})`);
        }
        // A stream already relayed its text; a plain JSON body relays it here, once.
        const text = streamedText ? "" : data.output.flatMap(item => item.type === "message"
          ? item.content.filter(block => block.type === "output_text").map(block => block.text) : []).join("");
        if (text) yield textEvent(text);
        const usage: Usage = {};
        for (const key of ["input_tokens", "output_tokens"] as const) {
          const count = data.usage?.[key];
          if (typeof count === "number") {
            usage[key] = count;
            total[key] = (total[key] ?? 0) + count;
          }
        }
        yield { type: "assistant", session_id, message: { model: data.model ?? opts.model, usage } };
        const calls = data.output.filter((item): item is FunctionCall => item.type === "function_call");
        if (!calls.length) {
          yield { type: "result", subtype: "success", session_id, usage: total, num_turns: turns };
          return;
        }
        input.push(...data.output);
        for (const call of calls) {
          if (signal.aborted) throw new Error("OpenAI request failed (network)");
          const output = await functionOutput(call, catalog, options.canUseTool);
          input.push({ type: "function_call_output", call_id: call.call_id, output });
        }
      }
      yield { type: "result", subtype: "error_max_turns", session_id, usage: total, num_turns: turns };
    },
  };
}

/** Who answered, carried on every usage event so the UI can badge the turn from the real runner. */
export interface ConverseProviderIdentity {
  /** "openai" (api.openai.com via env) or "openai_compatible" (a tenant's self-hosted server). */
  provider: "openai" | "openai_compatible";
  /** Where the model runs, e.g. "local on Mac mini". Optional. */
  label?: string;
}

export class OpenAiConverseRunner implements SpineConverseRunner {
  constructor(private readonly options: OpenAiOptions & Omit<ConverseSdkRunnerOptions, "sdkImport">
    & { identity?: ConverseProviderIdentity; promptBudgetChars?: number }) {}

  /** See SpineConverseRunner.promptBudgetChars: set for small-context self-hosted models. */
  get promptBudgetChars(): number | undefined {
    return this.options.promptBudgetChars;
  }

  async *run(input: ConverseRunInput) {
    const identity = this.options.identity ?? { provider: "openai" as const };
    for await (const event of this.runInner(input)) {
      yield event.type === "usage"
        ? { ...event, usage: { ...event.usage, provider: identity.provider,
            ...(identity.label ? { provider_label: identity.label } : {}) } }
        : event;
    }
  }

  private async *runInner(input: ConverseRunInput) {
    const { apiKey, model, fetchImpl, baseUrl, requestTimeoutMs: explicitTimeoutMs, identity: _identity,
      promptBudgetChars: _budget, ...hostOptions } = this.options;
    // A configured turn deadline also bounds each model request, so a long turn is never cut by
    // the shorter default request deadline. Unset keeps both defaults exactly.
    const requestTimeoutMs = explicitTimeoutMs
      ?? (hostOptions.turnTimeoutS !== undefined ? hostOptions.turnTimeoutS * 1000 : undefined);
    yield* new ConverseSdkRunner({
      ...hostOptions,
      // The vendor's schema factory uses the one-argument record(value) signature.
      zodImport: hostOptions.zodImport ?? (async () => ({
        z: { ...z, record: (value: z.ZodType) => z.record(z.string(), value) },
      })),
      sdkImport: async () => createOpenAiSdkFacade({
        apiKey, model, fetchImpl, baseUrl, ...(requestTimeoutMs !== undefined ? { requestTimeoutMs } : {}),
      }),
    }).run(input);
  }
}
