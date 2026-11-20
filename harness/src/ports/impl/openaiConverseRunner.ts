import { randomUUID } from "node:crypto";
import { z } from "zod";
import type { ConverseRunInput, SpineConverseRunner } from "../index.js";
import { ConverseSdkRunner, type ConverseSdkRunnerOptions } from "./converseSdkRunner.js";

interface OpenAiOptions {
  apiKey: string;
  model: string;
  fetchImpl?: typeof fetch;
  baseUrl?: string;
}

export function converseProviderFromEnv(env: NodeJS.ProcessEnv):
  { provider: "anthropic" } | { provider: "openai"; apiKey: string; model: string } {
  switch (env.LEAF_CONVERSE_PROVIDER) {
    case undefined:
    case "":
    case "anthropic":
      return { provider: "anthropic" };
    case "openai": {
      const apiKey = env.OPENAI_API_KEY?.trim();
      if (!apiKey) throw new Error("LEAF_CONVERSE_PROVIDER=openai needs OPENAI_API_KEY");
      return { provider: "openai", apiKey, model: env.LEAF_OPENAI_MODEL?.trim() || "gpt-5" };
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
      while (turns < (options.maxTurns ?? 25)) {
        const deadline = AbortSignal.timeout(120_000);
        const signal = options.abortController
          ? AbortSignal.any([deadline, options.abortController.signal]) : deadline;
        let response: Response;
        try {
          response = await (opts.fetchImpl ?? fetch)(endpoint, {
            method: "POST", redirect: "manual", signal,
            headers: { Authorization: `Bearer ${opts.apiKey}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              model: opts.model, instructions: options.systemPrompt, tools, input, store: false,
              include: ["reasoning.encrypted_content"],
            }),
          });
        } catch {
          throw new Error("OpenAI request failed (network)");
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
        try {
          data = await response.json() as ModelResponse;
        } catch {
          throw new Error("OpenAI request failed (network)");
        }
        if (!Array.isArray(data?.output) || (data.status && data.status !== "completed")) {
          throw new Error(`OpenAI request failed (${response.status})`);
        }
        const text = data.output.flatMap(item => item.type === "message"
          ? item.content.filter(block => block.type === "output_text").map(block => block.text) : []).join("");
        if (text) yield { type: "stream_event", session_id,
          event: { type: "content_block_delta", delta: { type: "text_delta", text } } };
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

export class OpenAiConverseRunner implements SpineConverseRunner {
  constructor(private readonly options: OpenAiOptions & Omit<ConverseSdkRunnerOptions, "sdkImport">) {}

  async *run(input: ConverseRunInput) {
    const { apiKey, model, fetchImpl, baseUrl, ...hostOptions } = this.options;
    yield* new ConverseSdkRunner({
      ...hostOptions,
      // The vendor's schema factory uses the one-argument record(value) signature.
      zodImport: hostOptions.zodImport ?? (async () => ({
        z: { ...z, record: (value: z.ZodType) => z.record(z.string(), value) },
      })),
      sdkImport: async () => createOpenAiSdkFacade({ apiKey, model, fetchImpl, baseUrl }),
    }).run(input);
  }
}
