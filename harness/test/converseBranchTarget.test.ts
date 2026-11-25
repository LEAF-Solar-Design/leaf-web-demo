import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { ConverseLoop } from "../src/agent/converseLoop.js";
import { SPINE_SYSTEM_PROMPT } from "../src/agent/spineSystemPrompt.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeConverseRunner } from "../src/ports/fakes/fakeConverseRunner.js";
import { FakeGateClient } from "../src/ports/fakes/fakeGateClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { ConverseSdkRunner } from "../src/ports/impl/converseSdkRunner.js";
import type { SpineConverseRunner } from "../src/ports/index.js";

const PACKET = {
  catalog: [],
  drawing: { id: "rooftop_demo", head_version: 3 },
  grant: { kind: "oauth", degraded: false },
};

describe("author_tool branch target", () => {
  it("preserves branch through approval replay and keeps Studio payloads and keys stable", async () => {
    const appRun = new FakeAppRunClient();
    const gate = new FakeGateClient();
    const store = new FakeSessionStore();
    const replayRunner = new FakeConverseRunner();
    let args: Record<string, unknown> = {};
    const runner: SpineConverseRunner = {
      async *run(input) {
        if (input.userMessage.includes("=== CONFIRMATION ===")) {
          yield* replayRunner.run(input);
          return;
        }
        await input.tools.execute("author_tool", args);
        yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
      },
    };
    const loop = new ConverseLoop({ runner, appRun, gate, store });
    const session = await loop.createOrGetSession("demo-tenant", "rooftop_demo");
    const base = { sessionId: session.session_id, tenantId: session.tenant_id, contextPacket: PACKET };
    const description = "count and label panels";
    const key = (branch = false) => `author:${createHash("sha256").update(JSON.stringify({
      action: "author_tool",
      tenant_id: session.tenant_id,
      session_id: session.session_id,
      description,
      ...(branch ? { target: "branch" } : {}),
    })).digest("hex")}`;

    for (const target of [undefined, "studio", "branch", "invalid"]) {
      args = { description, ...(target === undefined ? {} : { target }) };
      const previousCalls = appRun.authorCalls.length;
      const previousChecks = gate.checks.length;
      const turn = await loop.handleMessage({ ...base, text: description });
      await turn.done;
      expect(appRun.authorCalls).toHaveLength(previousCalls);
      const events = await store.eventsAfter(session.session_id, 0);
      const pending = events.filter(event => event.type === "confirmation_required").at(-1)!;
      const cid = String(pending.data.confirmation_id);
      const normalized = { description, ...(target === "branch" ? { target } : {}) };
      expect(gate.checks[previousChecks]!.args).toEqual(normalized);
      gate.grant(cid);
      const resumed = await loop.handleMessage({ ...base, confirm: { confirmationId: cid, approved: true } });
      await resumed.done;
      expect(appRun.authorCalls).toHaveLength(previousCalls + 1);
      expect(appRun.authorCalls.at(-1)).toEqual({
        tenantId: session.tenant_id, description, mode: "build", idempotencyKey: key(target === "branch"),
        ...(target === "branch" ? { target } : {}),
      });
      expect(gate.checks.at(-1)).toMatchObject({
        action: "author_tool", args: { ...normalized, confirmation_id: cid }, decision: "allow",
      });
    }
    expect(appRun.authorCalls[0]!.idempotencyKey).toBe(appRun.authorCalls[1]!.idempotencyKey);
    expect(appRun.authorCalls[2]!.idempotencyKey).not.toBe(appRun.authorCalls[0]!.idempotencyKey);
  });

  it.each([undefined, "branch"] as const)("sends the branch body key only for branch (%s)", async target => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response("{}", {
      headers: { "content-type": "application/json" },
    }));
    const client = new HttpAppRunClient({
      baseUrl: "https://app.invalid", dispatchSecret: "test-dispatch", fetchImpl: fetcher,
    });
    await client.authorTool("tenant", "count panels", "build", "request-key", undefined, "panel-counter", target);
    expect(fetcher).toHaveBeenCalledWith("https://app.invalid/api/author", expect.objectContaining({
      method: "POST",
      body: JSON.stringify({
        description: "count panels", mode: "build", target_tool_name: "panel-counter",
        ...(target === "branch" ? { target } : {}),
      }),
      headers: expect.objectContaining({ "idempotency-key": "request-key" }),
    }));
  });

  it("exposes AutoCAD routing guidance and the optional target enum to the SDK", async () => {
    const definitions: Array<{ name: string; description: string; schema: z.ZodRawShape }> = [];
    const runner = new ConverseSdkRunner({
      grant: { kind: "oauth", oauthToken: "fake-oauth" },
      model: "claude-sonnet-5",
      zodImport: () => import("zod"),
      sdkImport: async () => ({
        tool(name: string, description: string, schema: z.ZodRawShape) {
          const definition = { name, description, schema };
          definitions.push(definition);
          return definition;
        },
        createSdkMcpServer: () => ({}),
        async *query() {},
      }),
    });
    for await (const _event of runner.run({
      systemPrompt: SPINE_SYSTEM_PROMPT, userMessage: "count panels",
      tools: { list: () => ["author_tool"], execute: async () => ({ content: "ok" }) },
      canUseTool: async (_tool, input) => ({ behavior: "allow", updatedInput: input }),
    })) { /* Drain the mocked SDK turn. */ }
    const definition = definitions.find(tool => tool.name === "author_tool")!;
    expect(definition.description).toContain('"branch"');
    expect(definition.description).toContain("AutoCAD");
    expect(SPINE_SYSTEM_PROMPT).toContain('target: "branch"');
    const schema = z.object(definition.schema);
    for (const target of [undefined, "studio", "branch"]) {
      expect(schema.safeParse({ description: "count panels", ...(target ? { target } : {}) }).success).toBe(true);
    }
    expect(schema.safeParse({ description: "count panels", target: "invalid" }).success).toBe(false);
  });
});
