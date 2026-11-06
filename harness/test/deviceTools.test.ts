import { describe, expect, it, vi } from "vitest";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import {
  DEVICE_TOOL_GUIDANCE, DEVICE_TOOL_INPUTS, DEVICE_TOOL_OPERATIONS,
} from "../src/agent/deviceTools.js";
import type { DeviceToolName } from "../src/agent/deviceTools.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { ConverseSdkRunner } from "../src/ports/impl/converseSdkRunner.js";
import { z } from "zod";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import type { AppRunClient, ConverseRunInput, SpineConverseRunner, SpineToolResult } from "../src/ports/index.js";

const TENANT = "12345678-1234-4234-8234-123456789abc";
const AUTHORITY = { sessionId: "app-session", turnId: "app-turn" };
const CASES: Array<{ tool: DeviceToolName; args: Record<string, unknown> }> = [
  { tool: "device_list", args: {} },
  { tool: "device_install_package", args: { device_id: "home", package: "homeassistant" } },
  { tool: "device_ha_step", args: { device_id: "home", op: "ha.flow.step", args: { flow_id: "flow", input: {} } } },
  { tool: "device_light", args: { device_id: "home", action: "turn_on", entity_id: "light.desk", color: "#ff00ab", brightness_pct: 100 } },
  { tool: "device_light_states", args: { device_id: "home" } },
  { tool: "device_command_status", args: { device_id: "home", command_id: "command" } },
];

describe("device contract schemas", () => {
  it.each(CASES)("accepts $tool and rejects extra authority fields", ({ tool, args }) => {
    expect(DEVICE_TOOL_INPUTS[tool].parse(args)).toEqual(args);
    expect(DEVICE_TOOL_INPUTS[tool].safeParse({ ...args, authority: "forged" }).success).toBe(false);
  });

  it.each([
    { entity_id: "switch.desk" }, { entity_id: "light.Desk" }, { entity_id: "light." },
    { entity_id: `light.${"a".repeat(65)}` }, { entity_id: "light.desk\n" },
    { color: "red" }, { color: "#fff" }, { color: "#abcdef\n" },
    { brightness_pct: -1 }, { brightness_pct: 101 }, { brightness_pct: 2.5 },
    { brightness_pct: "50" }, { action: "toggle" },
  ])("rejects invalid light input %j", patch => {
    expect(DEVICE_TOOL_INPUTS.device_light.safeParse({ ...CASES[3].args, ...patch }).success).toBe(false);
  });

  it("accepts group boundaries and optional color and brightness", () => {
    expect(DEVICE_TOOL_INPUTS.device_light.parse({ device_id: "home", action: "turn_off", entity_id: `group.${"a".repeat(64)}`, brightness_pct: 0 })).toMatchObject({ brightness_pct: 0 });
    expect(DEVICE_TOOL_INPUTS.device_light.safeParse({ device_id: "home", action: "turn_off", entity_id: "light.a" }).success).toBe(true);
  });

  it.each(["ha.onboard", "ha.flow.start", "ha.flow.step", "ha.flow.list"])("accepts HA operation %s", op => {
    expect(DEVICE_TOOL_INPUTS.device_ha_step.safeParse({ device_id: "home", op, args: {} }).success).toBe(true);
  });

  it.each([
    { device_id: "home", op: "ha.restart", args: {} },
    { device_id: "home", op: "ha.onboard", args: [] },
    { device_id: "home", op: "ha.onboard", args: null },
    { device_id: "home", op: "ha.onboard" },
  ])("rejects invalid HA input %j", args => {
    expect(DEVICE_TOOL_INPUTS.device_ha_step.safeParse(args).success).toBe(false);
  });

  it.each(CASES.filter(entry => entry.tool !== "device_list"))("rejects missing required fields for $tool", ({ tool }) => {
    expect(DEVICE_TOOL_INPUTS[tool].safeParse({}).success).toBe(false);
  });
});

function clientWith(fetchImpl: typeof fetch) {
  return new HttpAppRunClient({ baseUrl: "https://studio.invalid/", dispatchSecret: "private-dispatch-value", fetchImpl });
}

describe("device HTTP client", () => {
  it.each(CASES)("posts $tool to the exact operation with authority headers", async ({ tool, args }) => {
    const response = { ok: true, result: { command_id: "command" } };
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(response), { headers: { "content-type": "application/json" } }));
    expect(await clientWith(request).deviceTool(TENANT, DEVICE_TOOL_OPERATIONS[tool], args, AUTHORITY)).toEqual(response);
    expect(request).toHaveBeenCalledExactlyOnceWith(`https://studio.invalid/internal/integrations/devices/${DEVICE_TOOL_OPERATIONS[tool]}`, {
      method: "POST", redirect: "error", signal: expect.any(AbortSignal),
      headers: { "content-type": "application/json", "x-tenant-id": TENANT, "x-dispatch-secret": "private-dispatch-value",
        "x-authority-session-id": AUTHORITY.sessionId, "x-authority-turn-id": AUTHORITY.turnId },
      body: JSON.stringify(args),
    });
  });

  it("relays the server's error message even on a non-2xx response", async () => {
    const response = { ok: false, error: { code: "conflict", message: "Press the bridge button and try again." } };
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(response), { status: 409 }));
    expect(await clientWith(request).deviceTool(TENANT, "list", {}, AUTHORITY)).toEqual(response);
  });

  it("enforces the 16 KiB request limit before transport", async () => {
    const request = vi.fn<typeof fetch>();
    const result = await clientWith(request).deviceTool(TENANT, "ha_step", {
      device_id: "home", op: "ha.onboard", args: { value: "a".repeat(16 * 1024) },
    }, AUTHORITY);
    expect(result).toMatchObject({ ok: false, error: { code: "invalid_input" } });
    expect(request).not.toHaveBeenCalled();
  });

  it("bounds streaming responses to the integration response cap", async () => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response("a".repeat(256 * 1024 + 1)));
    expect(await clientWith(request).deviceTool(TENANT, "list", {}, AUTHORITY)).toMatchObject({ ok: false, error: { code: "unavailable" } });
  });

  it("uses a total deadline even when fetch ignores cancellation", async () => {
    vi.useFakeTimers();
    try {
      const request = vi.fn<typeof fetch>(() => new Promise(() => {}));
      const result = clientWith(request).deviceTool(TENANT, "list", {}, AUTHORITY);
      await vi.advanceTimersByTimeAsync(15_000);
      expect(await result).toMatchObject({ ok: false, error: { code: "unavailable" } });
      expect((request.mock.calls[0][1]?.signal as AbortSignal).aborted).toBe(true);
      expect(request).toHaveBeenCalledTimes(1);
    } finally { vi.useRealTimers(); }
  });

  it.each(["malformed", JSON.stringify({ ok: true, result: { leaked: "private-dispatch-value" } })])("rejects malformed or secret-reflecting response", async body => {
    const request = vi.fn<typeof fetch>().mockResolvedValue(new Response(body));
    expect(await clientWith(request).deviceTool(TENANT, "list", {}, AUTHORITY)).toMatchObject({ ok: false, error: { code: "unavailable" } });
  });

  it("returns unavailable without authority or a dispatch secret", async () => {
    const request = vi.fn<typeof fetch>();
    expect(await clientWith(request).deviceTool(TENANT, "list", {}, { sessionId: "", turnId: "" })).toMatchObject({ error: { code: "unavailable" } });
    const client = new HttpAppRunClient({ baseUrl: "https://studio.invalid", dispatchSecret: "", fetchImpl: request });
    expect(await client.deviceTool(TENANT, "list", {}, AUTHORITY)).toMatchObject({ error: { code: "unavailable" } });
    expect(request).not.toHaveBeenCalled();
  });
});

function setup(tool: string, args: Record<string, unknown>) {
  const appRun = new FakeAppRunClient();
  appRun.deviceToolResult = { ok: true, result: { command_id: "command" } };
  const store = new FakeSessionStore();
  const gate = new FakeGateClient();
  const results: SpineToolResult[] = [];
  const runs: ConverseRunInput[] = [];
  const runner: SpineConverseRunner = {
    async *run(input) {
      runs.push(input);
      expect(await input.canUseTool(`mcp__spine__${tool}`, args)).toMatchObject({ behavior: "allow" });
      results.push(await input.tools.execute(`mcp__spine__${tool}`, args));
      yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
    },
  };
  const loop = new ConverseLoop({ appRun, store, gate, runner });
  const send = async (authority: { authoritySessionId?: string; authorityTurnId?: string } = {
    authoritySessionId: AUTHORITY.sessionId, authorityTurnId: AUTHORITY.turnId,
  }) => {
    const session = await loop.createOrGetSession(TENANT, "drawing");
    const turn = await loop.handleMessage({ tenantId: TENANT, sessionId: session.session_id, contextPacket: {}, text: "Control my lights", ...authority });
    await turn.done;
  };
  return { appRun, store, gate, results, runs, send };
}

describe("device tools are host gated", () => {
  it.each(CASES)("dispatches $tool once and records a tool row without a second gate", async ({ tool, args }) => {
    const state = setup(tool, args);
    await state.send();
    expect(state.appRun.deviceToolCalls).toEqual([{ tenantId: TENANT, operation: DEVICE_TOOL_OPERATIONS[tool], body: args, authority: AUTHORITY }]);
    expect(JSON.parse(state.results[0].content)).toEqual(state.appRun.deviceToolResult);
    expect(state.results[0].isError).not.toBe(true);
    expect(state.gate.checks).toHaveLength(0);
    expect([...state.store.events.values()].flat().filter(event => event.type === "tool_result").map(event => event.data)).toEqual([
      expect.objectContaining({ tool, ok: true, summary: "command command queued" }),
    ]);
  });

  it.each([{}, { authoritySessionId: "app-session" }, { authorityTurnId: "app-turn" }, { authoritySessionId: "bad\n", authorityTurnId: "turn" }])("returns unavailable with missing or malformed authority %j", async authority => {
    const state = setup("device_list", {});
    await state.send(authority);
    expect(state.appRun.deviceToolCalls).toHaveLength(0);
    expect(state.results[0].isError).toBe(true);
    expect(JSON.parse(state.results[0].content)).toMatchObject({ error: { code: "unavailable" } });
    expect(state.gate.checks).toHaveLength(0);
  });

  it("returns unavailable without a client method", async () => {
    const state = setup("device_list", {});
    (state.appRun as AppRunClient).deviceTool = undefined;
    await state.send();
    expect(JSON.parse(state.results[0].content)).toMatchObject({ error: { code: "unavailable" } });
  });

  it("relays an error result calmly with its message", async () => {
    const state = setup("device_list", {});
    state.appRun.deviceToolResult = { ok: false, error: { code: "denied", message: "Only the project owner can change this device." } };
    await state.send();
    expect(state.results[0].isError).toBe(true);
    expect(JSON.parse(state.results[0].content)).toEqual(state.appRun.deviceToolResult);
  });

  it("rejects argument injection before dispatch", async () => {
    const state = setup("device_list", { authority: "forged" });
    await state.send();
    expect(state.appRun.deviceToolCalls).toHaveLength(0);
    expect(JSON.parse(state.results[0].content)).toMatchObject({ error: { code: "invalid_input" } });
  });

  it("never relays arbitrary client exceptions or retries them", async () => {
    const state = setup("device_light", CASES[3].args);
    const request = vi.spyOn(state.appRun, "deviceTool").mockRejectedValue(new Error("private-value"));
    await state.send();
    expect(request).toHaveBeenCalledTimes(1);
    expect(JSON.parse(state.results[0].content)).toMatchObject({ error: { code: "unavailable" } });
    expect(JSON.stringify([...state.store.events.values()])).not.toContain("private-value");
  });

  it("appends home-device setup and state confirmation guidance to the system prompt", async () => {
    const state = setup("device_list", {});
    await state.send();
    expect(state.runs[0].systemPrompt).toContain(DEVICE_TOOL_GUIDANCE);
    expect(state.runs[0].systemPrompt).toContain("Ops, then Devices, then Link a device");
    expect(state.runs[0].systemPrompt).toContain("Always confirm by reading state after a change");
    for (const { tool } of CASES) expect(state.runs[0].systemPrompt).toContain(tool);
  });
});

describe("host-only device SDK tools", () => {
  it("mounts real schemas, delegates all six handlers, and preserves the permission hook", async () => {
    type MountedTool = {
      name: string; shape: z.ZodRawShape;
      handler: (args: Record<string, unknown>) => Promise<{ content: Array<{ type: string; text: string }>; isError?: boolean }>;
    };
    const tools: MountedTool[] = [];
    const queries: Array<{ canUseTool: ConverseRunInput["canUseTool"] }> = [];
    const sdk = {
      tool(name: string, _description: string, shape: z.ZodRawShape, handler: MountedTool["handler"]) {
        return { name, shape, handler };
      },
      createSdkMcpServer(options: { name: string; tools: MountedTool[] }) {
        if (options.name === "spine") tools.push(...options.tools);
        return options;
      },
      query({ options }: { options: { canUseTool: ConverseRunInput["canUseTool"] } }) {
        queries.push(options);
        return (async function* () {
          yield { type: "result", subtype: "success", num_turns: 1, session_id: "sdk-session",
            total_cost_usd: 0, usage: { input_tokens: 1, output_tokens: 1 } };
        })();
      },
    };
    const execute = vi.fn(async () => ({ content: "Device result", isError: true }));
    const permission = vi.fn<ConverseRunInput["canUseTool"]>().mockResolvedValue({ behavior: "deny", message: "policy" });
    const runner = new ConverseSdkRunner({
      grant: { kind: "oauth", oauthToken: "fake-oauth" }, model: "claude-sonnet-5",
      sdkImport: async () => sdk, zodImport: () => import("zod"),
    });
    const input: ConverseRunInput = {
      systemPrompt: "Device guidance", userMessage: "Lights",
      tools: { list: () => CASES.map(entry => entry.tool), execute }, canUseTool: permission,
    };
    for await (const _event of runner.run(input)) { /* drain */ }
    expect(tools.filter(tool => tool.name.startsWith("device_")).map(tool => tool.name)).toEqual(CASES.map(entry => entry.tool));
    for (const entry of CASES) {
      const tool = tools.find(tool => tool.name === entry.tool)!;
      expect(z.strictObject(tool.shape).parse(entry.args)).toEqual(entry.args);
      expect(await tool.handler(entry.args)).toEqual({ content: [{ type: "text", text: "Device result" }], isError: true });
      expect(execute).toHaveBeenLastCalledWith(entry.tool, entry.args);
      expect(await queries[0].canUseTool(`mcp__spine__${entry.tool}`, entry.args)).toMatchObject({ behavior: "deny" });
    }
    expect(permission).toHaveBeenCalledTimes(6);
    tools.length = 0;
    for await (const _event of runner.run({ ...input, tools: { ...input.tools, list: () => [] } })) { /* drain */ }
    expect(tools.some(tool => tool.name.startsWith("device_"))).toBe(false);
  });
});
