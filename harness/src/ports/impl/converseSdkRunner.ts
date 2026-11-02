// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
export * from "../../vendor/mushy-author/ports/impl/converseSdkRunner.js";

import {
  ConverseSdkRunner as VendorConverseSdkRunner,
} from "../../vendor/mushy-author/ports/impl/converseSdkRunner.js";
import type { ConverseSdkRunnerOptions } from "../../vendor/mushy-author/ports/impl/converseSdkRunner.js";
import type { ConverseRunInput, ConverseRunnerEvent } from "../index.js";
import { AuthorStageStatusInput, SPINE_TOOL_NAMES } from "../index.js";

export const SPINE_MCP_TOOL_NAMES = SPINE_TOOL_NAMES.map(name => `mcp__spine__${name}`);

type TextResult = { content: Array<{ type: "text"; text: string }>; isError?: boolean };
interface HostSdk {
  tool(name: string, description: string, shape: Record<string, unknown>,
    handler: (args: Record<string, unknown>) => Promise<TextResult>): unknown;
  createSdkMcpServer(options: Record<string, unknown>): unknown;
}

/** Each invocation owns its facade and captured executor, including overlapping runs. */
export class ConverseSdkRunner {
  constructor(private readonly options: ConverseSdkRunnerOptions) {}

  async *run(input: ConverseRunInput): AsyncIterable<ConverseRunnerEvent> {
    const sdkImport = this.options.sdkImport ??
      (() => import(["@anthropic-ai", "claude-agent-sdk"].join("/")));
    const zodImport = this.options.zodImport ?? (() => import(["zod"].join("/")));
    const runner = new VendorConverseSdkRunner({
      ...this.options,
      zodImport,
      sdkImport: async () => {
        const sdk = await sdkImport() as HostSdk;
        const enabled = input.tools.list().includes("propose_project_page");
        const { z } = enabled ? await zodImport() as {
          z: { string(): unknown; number(): unknown };
        } : { z: undefined };
        return new Proxy({} as HostSdk, {
          get(_facade, property) {
            const target = sdk;
            if (property === "query") return (query: { options: Record<string, unknown> }) => {
              const prior = query.options.canUseTool as
                (name: string, args: Record<string, unknown>) => Promise<unknown>;
              const actualQuery = Reflect.get(target, "query") as (value: unknown) => unknown;
              return actualQuery.call(target, { ...query, options: { ...query.options,
                canUseTool: async (name: string, args: Record<string, unknown>) => {
                  if (name === "author_stage_status" || name.endsWith("__author_stage_status")) {
                    if (name !== "mcp__spine__author_stage_status" ||
                        !AuthorStageStatusInput.safeParse(args).success) {
                      return { behavior: "deny", message: "Author stage status unavailable." };
                    }
                  }
                  return prior(name, args);
                },
              } });
            };
            if (property === "createSdkMcpServer") return (options: Record<string, unknown>) => {
              if (options.name !== "spine") return target.createSdkMcpServer(options);
              if (!Array.isArray(options.tools)) throw new Error("Invalid private tool catalog");
              const tools = [...options.tools];
              if (tools.some(tool => tool?.name === "author_stage_status")) {
                throw new Error("Duplicate author stage status tool");
              }
              if (input.tools.list().includes("author_stage_status")) tools.push(target.tool(
                "author_stage_status",
                "Read existing authoring stage status by change_set_id. Does not build, retry, approve, publish or run a tool.",
                AuthorStageStatusInput.shape,
                async args => {
                  const parsed = AuthorStageStatusInput.safeParse(args);
                  const failure: TextResult = { content: [{ type: "text", text: "Author stage status unavailable." }], isError: true };
                  if (!parsed.success) return failure;
                  try {
                    const result = await input.tools.execute("author_stage_status", parsed.data);
                    return { content: [{ type: "text", text: result.content }],
                      ...(result.isError !== undefined ? { isError: result.isError } : {}) };
                  } catch { return failure; }
                },
              ));
              if (tools.some(tool => tool?.name === "propose_project_page")) {
                throw new Error("Duplicate private proposal tool");
              }
              if (enabled && z) tools.push(target.tool(
                "propose_project_page",
                "Propose saved portfolio TEXT changes for private review. Use the current project context page revision and digest. Does not save or publish; unrelated to drawing overlays or customize_platform.",
                { instruction: z.string(), expected_revision: z.number(), expected_digest: z.string() },
                async args => {
                  const failure: TextResult = { content: [{ type: "text", text: "Portfolio proposal failed." }], isError: true };
                  try {
                    const result = await input.tools.execute("propose_project_page", args);
                    if (typeof result.content !== "string" || Buffer.byteLength(result.content, "utf8") > 4096 ||
                        (result.isError !== undefined && typeof result.isError !== "boolean")) return failure;
                    return { content: [{ type: "text", text: result.content }],
                      ...(result.isError !== undefined ? { isError: result.isError } : {}) };
                  } catch { return failure; }
                },
              ));
              return target.createSdkMcpServer({ ...options, tools });
            };
            const value: unknown = Reflect.get(target, property);
            return typeof value === "function" ? value.bind(target) : value;
          },
        });
      },
    });
    yield* runner.run(input);
  }
}
