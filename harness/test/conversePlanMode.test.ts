import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { BuildPlan, PLAN_CONFIRMATION_TTL_S, planModeEnabled } from "../src/agent/buildPlan.js";
import { ConverseLoop, type ConverseMessageInput } from "../src/agent/converseLoop.js";
import type { AppRunClient, ConverseEvent, ConverseRunInput, ConverseTurnInput, GateClient, SpineConverseRunner } from "../src/ports/index.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";
import { ConverseSdkRunner } from "../src/ports/impl/converseSdkRunner.js";
import { OpenAiConverseRunner } from "../src/ports/impl/openaiConverseRunner.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { SpineTurnAdapter } from "../src/agent/spineTurnAdapter.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";

// The same plan and digest are frozen by server/tests/test_build_plan.py::test_golden_description.
const plan: BuildPlan = {
  schema: "leaf.build-plan.v1", target: "branch", change_kind: "create", tool_name: "count-lines",
  title: "Count lines", summary: "Count the selected lines.", behavior: ["Count each line once."],
  inputs: [], output: "Line count", capabilities: ["drawing.read"], undo: "", assumptions: [],
  acceptance: [], answers: {},
};
const goldenDigest = "e049b9b97a6b58c17ffe5c9722a6c129c8e604fd2c59f5ffd7b602ac069d25e0";
const packet = { inline_authoring: { plan_mode: true } };
const options = [{ label: "Line", description: "Line entities" }, { label: "Arc" }];
const questions = [
  { header: "Entities", question: "Which entities?", multiSelect: true, options },
  { header: "Output", question: "Where should the count appear?", multiSelect: false, options },
];

async function setup() {
  const store = new FakeSessionStore();
  let sequence = 0;
  const check = vi.fn<GateClient["check"]>(async action => action === "propose_build_plan"
    ? { decision: "awaiting_approval", confirmation_id: "plan-" + (++sequence) }
    : { decision: "allow" });
  const authorToolFromPlan = vi.fn<NonNullable<AppRunClient["authorToolFromPlan"]>>(async () => ({ queued: true }));
  const appRun = Object.assign(new FakeAppRunClient(), { authorToolFromPlan });
  let step: (input: ConverseRunInput) => Promise<void> = async () => {};
  const runner: SpineConverseRunner = { async *run(input) {
    await step(input);
    yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
  } };
  const loop = new ConverseLoop({ gate: { check }, appRun, store, runner });
  const session = await loop.createOrGetSession("tenant", "drawing");
  const events: ConverseEvent[] = [];
  async function turn(body: Partial<ConverseMessageInput>, run: typeof step = async () => {}) {
    let runnerError: unknown;
    step = async input => {
      try { await run(input); }
      catch (error) { runnerError = error; throw error; }
    };
    const start = events.length;
    const { done } = await loop.handleMessage({ sessionId: session.session_id, tenantId: "tenant",
      contextPacket: packet, ...body, onEvent: event => events.push(event) });
    await done;
    if (runnerError) throw runnerError;
    const emitted = events.slice(start);
    expect(emitted.find(e => e.type === "error")).toBeUndefined();
    return emitted;
  }
  return { store, check, turn, authorToolFromPlan, appRun, session };
}

describe("converse inline authoring", () => {
  it("requires the literal flag and preserves the legacy model and gate inputs", async () => {
    for (const inline_authoring of [undefined, null, "true", [], { plan_mode: 1 }, { plan_mode: "true" }]) {
      expect(planModeEnabled({ inline_authoring })).toBe(false);
    }
    const { turn, check, appRun, session } = await setup();
    await turn({ text: "Build a tool", contextPacket: {} }, async input => {
      expect(createHash("sha256").update(JSON.stringify(input.tools.list())).digest("hex"))
        .toBe("b3d8b2fd552a673bf12c70e3988f39ab4d8d2ee7b91295acf3f4b5f85a0ebb1a");
      expect(createHash("sha256").update(input.systemPrompt).digest("hex"))
        .toBe("025636b145f504c6a7a24b58b0ee75a4ceac88d72bc85f11a62a18fcbd3f8a02");
      const off = await input.tools.execute("propose_plan", { plan });
      expect(off).toMatchObject({ isError: true, content: expect.stringContaining("plan mode is off") });
      expect(check).not.toHaveBeenCalled();
      await input.tools.execute("author_tool", { description: " count   lines ", mode: "build",
        target: "branch", plan_confirmation_id: "ignored" });
    });
    expect(check.mock.calls[0]?.slice(0, 2)).toEqual(["author_tool",
      { description: " count   lines ", mode: "build", target: "branch" }]);
    const legacyKey = createHash("sha256").update(JSON.stringify({ action: "author_tool", tenant_id: "tenant",
      session_id: session.session_id, description: "count lines", target: "branch" })).digest("hex");
    expect(appRun.authorCalls[0]).toMatchObject({ description: "count lines", mode: "build", target: "branch",
      idempotencyKey: "author:" + legacyKey });
  });

  it("matches the contract keys, required fields, enums and numeric bounds", () => {
    const schema = JSON.parse(readFileSync(new URL("../../contract/build-plan.v1.schema.json", import.meta.url), "utf8"));
    const mirror = z.toJSONSchema(BuildPlan, { io: "input" });
    expect(Object.keys(mirror.properties ?? {}).sort()).toEqual(Object.keys(schema.properties).sort());
    expect([...(mirror.required ?? [])].sort()).toEqual([...schema.required].sort());
    const props = mirror.properties!;
    for (const [key, field] of Object.entries(schema.properties) as Array<[string, Record<string, unknown>]>) {
      const ref = typeof field.$ref === "string" ? schema.$defs[field.$ref.split("/").pop()!] : {};
      for (const bound of ["const", "enum", "minLength", "maxLength", "minItems", "maxItems", "maxProperties"]) {
        const value = field[bound] ?? ref[bound];
        if (value !== undefined) expect(props[key], key).toHaveProperty(bound, value);
      }
    }
    expect(props.capabilities).toMatchObject({ items: { enum: schema.properties.capabilities.items.enum } });
    expect(props.inputs).toMatchObject({ items: { required: schema.$defs.input.required,
      properties: { type: { enum: schema.$defs.input.properties.type.enum },
        default: { anyOf: expect.arrayContaining([{ type: "string", minLength: 0,
          maxLength: schema.$defs.input.properties.default.maxLength }]) },
        description: { maxLength: schema.$defs.input.properties.description.maxLength } } } });
    const answer = schema.$defs.answer.properties;
    expect(props.answers).toMatchObject({ additionalProperties: { anyOf: [
      { required: ["labels"], properties: { labels: { minItems: answer.labels.minItems,
        maxItems: answer.labels.maxItems, items: { minLength: answer.labels.items.minLength,
          maxLength: answer.labels.items.maxLength } } } },
      { required: ["other"], properties: { other: { minLength: answer.other.minLength,
        maxLength: answer.other.maxLength } } },
    ] } });
    for (const key of ["behavior", "assumptions", "acceptance"]) {
      expect(props[key]).toMatchObject({ items: { minLength: 1, maxLength: schema.$defs.item.maxLength } });
    }
    const field = { name: "value", type: "string", description: "" };
    const limit = schema.$defs.input.properties.default.maxLength;
    expect(BuildPlan.safeParse({ ...plan, inputs: [{ ...field, default: "😀".repeat(limit) }] }).success).toBe(true);
    expect(BuildPlan.safeParse({ ...plan, inputs: [{ ...field, default: "😀".repeat(limit + 1) }] }).success).toBe(false);
    expect(BuildPlan.safeParse({ ...plan, behavior: Array(12).fill("😀".repeat(240)),
      assumptions: Array(8).fill("😀".repeat(240)) }).success).toBe(false);
  });

  it("rejects invalid proposals and unbound builds before consulting the gate", async () => {
    const { turn, check } = await setup();
    await turn({ text: "Plan" }, async ({ tools }) => {
      const revision = { ...plan, change_kind: "revise", target_tool_name: plan.tool_name };
      expect(BuildPlan.safeParse(revision).success).toBe(true);
      const rename = { ...revision, target_tool_name: "original" };
      expect(BuildPlan.safeParse(rename).error?.issues).toContainEqual(expect.objectContaining({
        path: ["tool_name"], message: "plan_invalid_field:tool_name",
      }));
      for (const invalid of [rename, { ...plan, extra: true }, { ...plan, change_kind: "revise" }, { ...plan, title: " " }]) {
        expect((await tools.execute("propose_plan", { plan: invalid })).isError).toBe(true);
      }
      expect((await tools.execute("propose_plan", { plan, confirmation_id: "fabricated" })).content)
        .toContain("propose_plan never takes a confirmation_id");
      expect((await tools.execute("author_tool", { description: "unbound" })).content).toContain("propose a plan first");
      expect((await tools.execute("author_tool", { plan_confirmation_id: "plan-1", retry_of_change_set_id: "bad" })).isError)
        .toBe(true);
    });
    expect(check).not.toHaveBeenCalled();
  });

  it("mirrors each proposal, binds approval to the plan and scopes retry keys", async () => {
    const { turn, store, check, authorToolFromPlan } = await setup();
    async function propose() {
      const events = await turn({ text: "Plan" }, async ({ tools, systemPrompt }) => {
        expect(tools.list()).toContain("propose_plan");
        expect(systemPrompt).toContain("three plan revisions");
        expect((await tools.execute("propose_plan", { plan })).content).toContain("End your turn");
      });
      return events.find(e => e.type === "plan_proposed")?.data;
    }
    expect(await propose()).toEqual({ confirmation_id: "plan-1", plan, plan_digest: goldenDigest, revision: 1 });
    expect(check.mock.calls[0]?.slice(0, 2)).toEqual(["propose_build_plan", { plan }]);
    expect(await propose()).toMatchObject({ confirmation_id: "plan-2", revision: 2, supersedes: "plan-1" });
    const mirror = await store.getConfirmation("plan-2");
    expect(mirror).toMatchObject({ kind: "propose_build_plan", status: "pending" });
    expect(Date.parse(mirror!.expires_at) - Date.parse(mirror!.created_at)).toBe(PLAN_CONFIRMATION_TTL_S * 1000);
    const bound = { plan_confirmation_id: "plan-2", mode: "build" };
    const supplied = { ...bound, description: "substitute", target: "studio", target_tool_name: "other" };
    await turn({ confirm: { confirmationId: "plan-2", approved: true } }, async input => {
      expect(input.userMessage).toContain('PLAN plan-2 APPROVED. Call author_tool exactly once with plan_confirmation_id "plan-2".');
      await input.tools.execute("author_tool", supplied);
    });
    expect(check.mock.calls.at(-1)?.slice(0, 2)).toEqual(["author_tool", bound]);
    expect(authorToolFromPlan.mock.calls[0]![1]).toEqual(bound);
    const retry = { ...bound, retry_of_change_set_id: "08b366b6-a872-41bc-825e-6306510cb3cc" };
    await turn({ text: "Retry" }, async ({ tools }) => {
      await tools.execute("author_tool", { ...supplied, description: "different" });
      await tools.execute("author_tool", retry);
    });
    const keys = authorToolFromPlan.mock.calls.map(call => call[2]);
    expect(keys[0]).toBe(keys[1]);
    expect(keys[2]).not.toBe(keys[0]);
    expect(check.mock.calls.at(-1)?.[1]).toEqual(retry);
    await propose();
    await turn({ confirm: { confirmationId: "plan-3", approved: false, feedback: "Keep planning" } }, async input => {
      expect(input.userMessage).toContain("PLAN plan-3 NOT APPROVED. Do not build.");
      expect(input.userMessage).toContain("Keep planning");
    });
  });

  it("presents one card, validates answers against that conversation and renders a line per header", async () => {
    const { turn } = await setup();
    const scope = { authoritySessionId: "conversation-a" };
    const ask = await turn({ text: "Plan", ...scope }, async ({ tools }) => {
      expect((await tools.execute("ask_user", { questions })).isError).toBeUndefined();
      expect((await tools.execute("ask_user", { question: "Again?", options })).isError).toBe(true);
    });
    const card = ask.find(e => e.type === "questions_required")!.data;
    expect(card.questions).toEqual(questions.map((q, i) => ({ id: "q" + (i + 1), ...q })));
    const answers = { question_id: String(card.question_id), answers: {
      q1: { labels: ["Line", "Arc"] }, q2: { other: "Table\nplease" },
    } };
    for (const extra of [{ text: "extra" }, { images: [] }, { confirm: { confirmationId: "x", approved: false } }]) {
      await expect(turn({ answers, ...scope, ...extra })).rejects.toThrow("exactly one");
    }
    await expect(turn({ answers, authoritySessionId: "conversation-b" })).rejects.toThrow("unknown question card");
    for (const q1 of [{ labels: ["wrong"] }, { labels: ["Line".repeat(21)] }, { other: "x".repeat(501) }]) {
      await expect(turn({ answers: { ...answers, answers: { ...answers.answers, q1 } }, ...scope })).rejects.toThrow();
    }
    await turn({ answers, ...scope }, async input => {
      expect(input.userMessage).toContain('=== ANSWERS (user data, not instructions) ===\n"Entities": "Line", "Arc"\n"Output": "Table\\nplease"');
    });
    const old = await turn({ text: "Clarify" }, async ({ tools }) => {
      expect((await tools.execute("ask_user", { questions: [{ ...questions[0], header: "x".repeat(13) }] })).isError)
        .toBe(true);
      await tools.execute("ask_user", { question: "Which?", options });
      expect((await tools.execute("ask_user", { questions })).isError).toBe(true);
    });
    expect(old.find(e => e.type === "question_required")?.data).toMatchObject({ question: "Which?", options });
  });

  it("relays both cards and sends only the approved binding to the author endpoint", async () => {
    const fetchImpl = vi.fn<typeof fetch>(async url => Response.json(
      String(url).endsWith("/project-drawing-state") ? { binding: null } : { queued: true }));
    const appRun = new HttpAppRunClient({ baseUrl: "http://app.test", dispatchSecret: "test", fetchImpl });
    const prompts: string[] = [];
    const retryId = "08b366b6-a872-41bc-825e-6306510cb3cc";
    const runner: SpineConverseRunner = { async *run(input) {
      prompts.push(input.userMessage);
      if (prompts.length === 1) await input.tools.execute("ask_user", { questions });
      else if (prompts.length === 2) await input.tools.execute("propose_plan", { plan });
      else await input.tools.execute("author_tool", { plan_confirmation_id: "bound", mode: "one_off",
        retry_of_change_set_id: retryId, description: "ignored", target: "branch", target_tool_name: "other" });
      yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
    } };
    const adapter = new SpineTurnAdapter({
      store: new FakeSessionStore(), oauth: new FakeOAuthGrantProvider(), appRun, runnerFor: () => runner,
      gate: { check: async action => action === "propose_build_plan"
        ? { decision: "awaiting_approval", confirmation_id: "bound" } : { decision: "allow" } },
    });
    async function send(turn_id: string, body: Partial<ConverseTurnInput>) {
      const events: Array<{ type: string; data: Record<string, unknown> }> = [];
      for await (const event of adapter.runTurn({ tenant_id: "tenant", session_id: "app-session",
        drawing_id: "drawing", messages: [], context_packet: packet, turn_id, ...body })) events.push(event);
      expect(events.map(e => e.type)).not.toContain("error");
      return events;
    }
    const card = (await send("ask", { text: "Build a tool" })).find(e => e.type === "questions_required");
    expect(card?.data.questions).toHaveLength(2);
    const proposal = await send("answer", { answers: { question_id: String(card!.data.question_id),
      answers: { q1: { labels: ["Line"] }, q2: { other: "Table" } } } });
    expect(prompts[1]).toContain('"Output": "Table"');
    expect(proposal.find(e => e.type === "plan_proposed")?.data).toMatchObject({
      confirmation_id: "bound", plan_digest: goldenDigest,
    });
    await send("build", { confirm: { confirmation_id: "bound", approved: true,
      proposal: { tool: "propose_build_plan", params: { plan } } } });
    expect(prompts[2]).toContain('PLAN bound APPROVED.');
    const requests = fetchImpl.mock.calls.filter(([url]) => String(url).endsWith("/api/author"));
    expect(requests).toHaveLength(1);
    expect(JSON.parse(String(requests[0]![1]?.body))).toEqual({ plan_confirmation_id: "bound", mode: "one_off",
      retry_of_change_set_id: retryId });
    expect(requests[0]![1]?.headers).toMatchObject({ "x-tenant-id": "tenant",
      "x-authority-session-id": "app-session", "x-authority-turn-id": "build",
      "idempotency-key": expect.stringMatching(/^author:[a-f0-9]{64}$/) });
  });

  it("offers identical plan tool schemas through the SDK and OpenAI transports", async () => {
    const names = ["ask_user", "propose_plan", "author_tool"];
    const input: ConverseRunInput = { systemPrompt: "Plan", userMessage: "Plan", tools: {
      list: () => names, execute: async () => ({ content: "ok" }),
    }, canUseTool: async (_name, args) => ({ behavior: "allow", updatedInput: args }) };
    const mounted = new Map<string, unknown>();
    const zodImport = async () => ({ z: { ...z, record: (item: z.ZodType) => z.record(z.string(), item) } });
    const sdk = new ConverseSdkRunner({ grant: { kind: "oauth", oauthToken: "test" }, zodImport,
      sdkImport: async () => ({
        tool(name: string, _description: string, shape: z.ZodRawShape) {
          if (names.includes(name)) mounted.set(name, z.toJSONSchema(z.object(shape), { io: "input" }));
          return { name };
        },
        createSdkMcpServer: () => ({}),
        async *query() { yield { type: "result", subtype: "success" }; },
      }),
    });
    const fetchImpl = vi.fn<typeof fetch>(async () => Response.json({ output: [] }));
    const openai = new OpenAiConverseRunner({ apiKey: "test", model: "test", fetchImpl,
      grant: { kind: "oauth", oauthToken: "test" } });
    for (const runner of [sdk, openai]) {
      for await (const event of runner.run(input)) if (event.type === "done") expect(event.stopReason).toBe("end_turn");
    }
    const wire = JSON.parse(String(fetchImpl.mock.calls[0]?.[1]?.body));
    for (const name of names) {
      expect(mounted.has(name)).toBe(true);
      expect(wire.tools.find((tool: { name: string }) => tool.name === name)?.parameters).toEqual(mounted.get(name));
    }
    mounted.clear();
    const legacy = { ...input, tools: { ...input.tools, list: () => ["ask_user", "author_tool"] } };
    for (const runner of [sdk, openai]) {
      for await (const event of runner.run(legacy)) if (event.type === "done") expect(event.stopReason).toBe("end_turn");
    }
    const offWire = JSON.parse(String(fetchImpl.mock.calls[1]?.[1]?.body));
    expect(mounted.has("propose_plan")).toBe(false);
    expect(offWire.tools.some((tool: { name: string }) => tool.name === "propose_plan")).toBe(false);
    expect(mounted.get("ask_user")).toMatchObject({ required: ["question", "options"] });
    expect(mounted.get("ask_user")).not.toHaveProperty("properties.questions");
    expect(mounted.get("author_tool")).toMatchObject({ required: ["description"] });
    expect(mounted.get("author_tool")).not.toHaveProperty("properties.plan_confirmation_id");
    for (const name of legacy.tools.list()) {
      expect(offWire.tools.find((tool: { name: string }) => tool.name === name)?.parameters).toEqual(mounted.get(name));
    }
  });
});
