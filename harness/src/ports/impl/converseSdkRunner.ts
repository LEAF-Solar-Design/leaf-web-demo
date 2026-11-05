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
import { SPINE_TOOL_NAMES } from "../index.js";
import { DEVICE_TOOL_INPUTS, DEVICE_TOOL_OPERATIONS } from "../../agent/deviceTools.js";
import type { DeviceToolName } from "../../agent/deviceTools.js";

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
            if (property === "createSdkMcpServer") return (options: Record<string, unknown>) => {
              if (options.name !== "spine") return target.createSdkMcpServer(options);
              if (!Array.isArray(options.tools)) throw new Error("Invalid private tool catalog");
              const tools = [...options.tools];
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
              for (const name of Object.keys(DEVICE_TOOL_INPUTS) as DeviceToolName[]) {
                if (tools.some(tool => tool?.name === name)) throw new Error("Duplicate private device tool");
                if (!input.tools.list().includes(name)) continue;
                tools.push(target.tool(
                  name, `Home device operation: ${DEVICE_TOOL_OPERATIONS[name]}. Commands are asynchronous; use device_command_status to poll.`,
                  DEVICE_TOOL_INPUTS[name].shape,
                  async args => {
                    try {
                      const result = await input.tools.execute(name, args);
                      if (typeof result.content !== "string" || Buffer.byteLength(result.content, "utf8") > 256 * 1024 ||
                          (result.isError !== undefined && typeof result.isError !== "boolean")) throw new Error("Invalid device result");
                      return { content: [{ type: "text", text: result.content }],
                        ...(result.isError !== undefined ? { isError: result.isError } : {}) };
                    } catch {
                      return { content: [{ type: "text", text: "Device tools are unavailable. Please try again later." }], isError: true };
                    }
                  },
                ));
              }
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
