import { describe, expect, it, vi } from "vitest";
import { z } from "zod";
import { ConverseLoop } from "../src/agent/converseLoop.js";
import {
  ProjectPhotoAssignmentRequest, PHOTO_ASSIGNMENT_FAILURE, PHOTO_ASSIGNMENT_SUMMARY, SPINE_TOOL_NAMES,
} from "../src/ports/index.js";
import type {
  AppRunClient, ConverseEvent, ConverseRunInput, GateClient, GateCheckResult, SpineConverseRunner, SpineToolResult,
} from "../src/ports/index.js";
import { HttpAppRunClient } from "../src/ports/impl/appRunClient.js";
import { ConverseSdkRunner, SPINE_MCP_TOOL_NAMES } from "../src/ports/impl/converseSdkRunner.js";
import { FakeAppRunClient } from "../src/ports/fakes/fakeAppRunClient.js";
import { FakeSessionStore } from "../src/ports/fakes/fakeSessionStore.js";

const TOOL = "propose_project_photo_assignments";
const TENANT = "11111111-1111-4111-8111-111111111111";
const PROJECT = "22222222-2222-4222-8222-222222222222";
const mid = (n: number) => `33333333-3333-4333-8333-${String(n).padStart(12, "0")}`;
const SECRET = "private-photo-dispatch-secret";
const AUTHORITY = { sessionId: "actual/app-session", turnId: "actual-app-turn" };
const source = { project_id: PROJECT, revision: 7, digest: "a".repeat(64) };
const operation = (n: number, gallery_slug = "cakes") => ({
  op: "assign" as const, material_id: mid(n), sha256: "b".repeat(64), preview_sha256: "c".repeat(64),
  gallery_slug, title: `Cake ${n}`, alt: `A decorated cake ${n}`,
});
const request = () => ({ expected_revision: source.revision, expected_digest: source.digest,
  operations: [operation(1), operation(2), operation(3, "cookies"), operation(4)] });
const packet = () => ({ project_material_context: {
  schema: "leaf.project-material-context.v1", org_id: TENANT, project_id: PROJECT,
  page: { revision: source.revision, digest: source.digest },
} });
function producer() {
  const operations = request().operations;
  const image = ({ material_id, sha256, preview_sha256, title, alt }: ReturnType<typeof operation>) =>
    ({ material_id, sha256, preview_sha256, title, alt });
  return {
    schema: "leaf.project-photo-assignment-proposal.v1", source: { ...source }, proposed_digest: "d".repeat(64),
    proposed_document: {
      schema: "leaf.portfolio.v1", name: "Jessica", headline: "Selected baking", introduction: "", about: "",
      contact: { email: "", location: "" }, resume: [], theme: "light", galleryLayout: "grid",
      appearance: { font: "sans", palette: { background: "#ffffff" } },
      categories: [
        { slug: "cakes", title: "Cakes", description: "", hero: 0,
          images: [image(operation(99)), ...operations.filter(op => op.gallery_slug === "cakes").map(image)] },
        { slug: "cookies", title: "Cookies", description: "", hero: 0,
          images: operations.filter(op => op.gallery_slug === "cookies").map(image) },
        { slug: "bread", title: "Bread", description: "", hero: 0, images: [] },
      ],
    },
    changes: operations.map((op, index) => ({ op: op.op, material_id: op.material_id, material_name: `upload-${index}.jpg`,
      gallery_slug: op.gallery_slug, gallery_title: op.gallery_slug === "cakes" ? "Cakes" : "Cookies",
      index: [1, 2, 0, 3][index]!, title: op.title, alt: op.alt })),
  };
}
const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
function http(value: unknown = producer()) {
  const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse(value));
  const client = new HttpAppRunClient({ baseUrl: "https://app.invalid/", dispatchSecret: SECRET, fetchImpl });
  return { client, fetchImpl };
}
type Mounted = { name: string; shape: z.ZodRawShape;
  handler: (args: Record<string, unknown>) => Promise<{ content: Array<{ type: string; text?: string }>; isError?: boolean }> };
function sdkFixture(invoke?: (input: ConverseRunInput["canUseTool"], tools: Mounted[]) => Promise<void>) {
  const mounted: Mounted[] = [];
  const sdk = {
    tool(name: string, _description: string, shape: z.ZodRawShape, handler: Mounted["handler"]) { return { name, shape, handler }; },
    createSdkMcpServer(options: { name: string; tools: Mounted[] }) {
      if (options.name === "spine") mounted.push(...options.tools);
      return options;
    },
    query({ options }: { options: { canUseTool: ConverseRunInput["canUseTool"] } }) {
      return (async function* () {
        await invoke?.(options.canUseTool, mounted);
        yield { type: "result", subtype: "success", is_error: false, result: "Private review ready." };
      })();
    },
  };
  const runner = new ConverseSdkRunner({ grant: { kind: "oauth", oauthToken: "fake-agent-grant" },
    sdkImport: async () => sdk, zodImport: async () => ({ z }) });
  return { sdk, runner, mounted };
}
interface ConversationOptions {
  args?: Record<string, unknown>; context?: Record<string, unknown>; noAuthority?: boolean;
  authority?: { sessionId: string; turnId: string }; verdict?: GateCheckResult; plain?: boolean; alias?: string;
}
async function conversation(appRun: AppRunClient, options: ConversationOptions = {}) {
  const replies: unknown[] = [], permissions: unknown[] = [], modelResults: SpineToolResult[] = [];
  const args = options.args ?? request();
  const check = vi.fn<GateClient["check"]>().mockResolvedValue(options.verdict ?? { decision: "allow" });
  const fixture = sdkFixture(async (hook, mounted) => {
    const tool = mounted.find(item => item.name === TOOL)!;
    permissions.push(await hook(options.alias ?? `mcp__spine__${TOOL}`, args));
    // Deliberately bypass SDK validation too: the handler must still fail closed.
    replies.push(await tool.handler(args));
  });
  const direct: SpineConverseRunner = { async *run(input) {
    permissions.push(await input.canUseTool(options.alias ?? TOOL, args));
    modelResults.push(await input.tools.execute(options.alias ?? TOOL, args));
    yield { type: "done", stopReason: "end_turn", sdkSessionId: null };
  } };
  const store = new FakeSessionStore();
  const loop = new ConverseLoop({ appRun, gate: { check }, store, runner: options.plain ? direct : fixture.runner });
  const session = await loop.createOrGetSession(TENANT, "unrelated-drawing");
  const emitted: ConverseEvent[] = [];
  const authority = options.authority ?? AUTHORITY;
  const turn = await loop.handleMessage({ sessionId: session.session_id, tenantId: TENANT,
    text: "Propose these inspected photos for review", contextPacket: options.context ?? packet(),
    ...(options.noAuthority ? {} : { authoritySessionId: authority.sessionId, authorityTurnId: authority.turnId }),
    onEvent: event => emitted.push(event) });
  await turn.done;
  return { session, emitted, events: await store.eventsAfter(session.session_id, 0), check,
    replies, permissions, modelResults, mounted: fixture.mounted };
}

describe("closed photo assignment caller through SDK, loop and HTTP", () => {
  it("mounts once and emits the unmodified multi-photo producer payload on the normal durable envelope", async () => {
    const value = producer();
    const { client, fetchImpl } = http(value);
    const result = await conversation(client);
    expect(SPINE_TOOL_NAMES).toContain(TOOL);
    expect(SPINE_MCP_TOOL_NAMES).toContain(`mcp__spine__${TOOL}`);
    const tools = result.mounted.filter(tool => tool.name === TOOL);
    expect(tools).toHaveLength(1);
    expect(Object.keys(tools[0]!.shape)).toEqual(["expected_revision", "expected_digest", "operations"]);
    expect(z.strictObject(tools[0]!.shape).parse(request())).toEqual(request());
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "allow" })]);
    expect(result.replies).toEqual([{ content: [{ type: "text", text: PHOTO_ASSIGNMENT_SUMMARY }] }]);
    expect(result.check).toHaveBeenCalledExactlyOnceWith(TOOL, request(), expect.objectContaining({
      tenantId: TENANT, sessionId: result.session.session_id,
      authoritySessionId: AUTHORITY.sessionId, authorityTurnId: AUTHORITY.turnId,
    }));
    expect(fetchImpl).toHaveBeenCalledExactlyOnceWith(
      "https://app.invalid/api/sessions/actual%2Fapp-session/page/photo-assignment-proposals",
      expect.objectContaining({ method: "POST", redirect: "error", body: JSON.stringify(request()), headers: {
        "content-type": "application/json", "x-tenant-id": TENANT, "x-dispatch-secret": SECRET,
        "x-authority-session-id": AUTHORITY.sessionId, "x-authority-turn-id": AUTHORITY.turnId,
      } }),
    );
    const output = result.emitted.filter(event => event.type === "tool_result" && event.data.tool === TOOL);
    expect(output).toHaveLength(1);
    expect(output[0]).toMatchObject({ session_id: result.session.session_id, seq: expect.any(Number),
      turn_id: expect.any(String), data: { tool: TOOL, ok: true, result: value } });
    expect(output[0]!.turn_id).not.toBe(AUTHORITY.turnId);
    expect(result.events.filter(event => event.type === "tool_result").map(({ session_id, turn_id, seq, data }) =>
      ({ session_id, turn_id, seq, data }))).toEqual(output.map(({ session_id, turn_id, seq, data }) =>
      ({ session_id, turn_id, seq, data })));
    expect(JSON.stringify(result.events)).not.toContain(SECRET);
    expect(JSON.stringify(result.replies)).not.toContain("material_id");
    expect(output[0]!.data.result).not.toHaveProperty("model");
  });

  it("also dispatches the plain host tool through the same policy", async () => {
    const { client, fetchImpl } = http();
    const result = await conversation(client, { plain: true });
    expect(result.modelResults).toEqual([{ content: PHOTO_ASSIGNMENT_SUMMARY }]);
    expect(result.check).toHaveBeenCalledOnce();
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  const badArgs: Record<string, unknown>[] = [
    {}, { ...request(), expected_revision: 0 }, { ...request(), expected_revision: 2147483648 },
    { ...request(), expected_revision: "7" }, { ...request(), expected_revision: 1.5 },
    { ...request(), expected_digest: "A".repeat(64) }, { ...request(), expected_digest: `${source.digest}\n` },
    { ...request(), operations: [] }, { ...request(), operations: Array.from({ length: 201 }, (_, n) => operation(n)) },
    { ...request(), operations: [operation(1), operation(1)] }, { ...request(), tenant_id: TENANT },
    ...["project_id", "session_id", "authority", "turn_id", "url", "callback", "proposed_document"].map(key => ({ ...request(), [key]: "forged" })),
    ...[
      { op: "remove" }, { material_id: "00000000-0000-0000-0000-000000000000" },
      { material_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa".toUpperCase() }, { sha256: "b".repeat(63) },
      { preview_sha256: "C".repeat(64) }, { gallery_slug: "Bad/slug" }, { title: "<b>cake</b>" },
      { title: "https://evil.invalid" }, { title: "www.evil.invalid" }, { title: "x\u200b" },
      { title: "\ud800" }, { title: "x".repeat(201) }, { alt: "" }, { alt: " " }, { alt: "x\u0000" },
      { alt: "x".repeat(401) }, { url: "https://evil.invalid" },
    ].map(change => ({ ...request(), operations: [{ ...operation(1), ...change }] })),
    // Every individual string fits the code-point bounds, but the UTF-8 JSON exceeds 256 KiB.
    { ...request(), operations: Array.from({ length: 200 }, (_, n) => ({ ...operation(n), title: "😀".repeat(200), alt: "😀".repeat(400) })) },
  ];
  it.each(badArgs)("rejects strict input case %# before gate and transport", async args => {
    expect(ProjectPhotoAssignmentRequest.safeParse(args).success).toBe(false);
    const { client, fetchImpl } = http();
    const result = await conversation(client, { args });
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "deny" })]);
    expect(result.replies).toEqual([{ content: [{ type: "text", text: PHOTO_ASSIGNMENT_FAILURE }], isError: true }]);
    expect(result.check).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
    // A direct host call must enforce the same contract without the SDK facade.
    const direct = await conversation(client, { args, plain: true });
    expect(direct.modelResults[0]?.isError).toBe(true);
    expect(direct.check).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("accepts safe Unicode/newlines without coercing literal text", () => {
    const body = { ...request(), operations: [{ ...operation(1), title: "", alt: "Crème 🎂\nCake" }] };
    expect(ProjectPhotoAssignmentRequest.parse(body)).toEqual(body);
  });

  it.each(["absent", "schema", "tenant", "project", "revision", "digest", "authority", "invalid-authority", "missing-session", "missing-turn", "missing-method"])(
    "rejects unavailable trusted source or authority (%s) before policy/HTTP", async kind => {
      const context = packet();
      if (kind === "schema") context.project_material_context.schema = "foreign-context";
      if (kind === "tenant") context.project_material_context.org_id = PROJECT;
      if (kind === "project") context.project_material_context.project_id = "bad-project";
      if (kind === "revision") context.project_material_context.page.revision++;
      if (kind === "digest") context.project_material_context.page.digest = "f".repeat(64);
      const { client, fetchImpl } = http();
      const result = await conversation(kind === "missing-method" ? new FakeAppRunClient() : client, {
        context: kind === "absent" ? {} : context, noAuthority: kind === "authority",
        ...(kind === "invalid-authority" ? { authority: { ...AUTHORITY, turnId: "bad\nturn" } } : {}),
        ...(kind === "missing-session" ? { authority: { ...AUTHORITY, sessionId: "" } } : {}),
        ...(kind === "missing-turn" ? { authority: { ...AUTHORITY, turnId: "" } } : {}),
      });
      expect(result.check).not.toHaveBeenCalled();
      expect(fetchImpl).not.toHaveBeenCalled();
      expect(result.events.some(event => Object.hasOwn(event.data, "result"))).toBe(false);
      expect(result.replies).toEqual([{ content: [{ type: "text", text: PHOTO_ASSIGNMENT_FAILURE }], isError: true }]);
    },
  );

  it.each<GateCheckResult>([
    { decision: "deny", reason: SECRET }, { decision: "awaiting_approval" },
    { decision: "awaiting_approval", confirmation_id: "0123456789abcdef", reason: SECRET },
  ])("never dispatches or projects a non-allow policy verdict %#", async verdict => {
    const { client, fetchImpl } = http();
    const result = await conversation(client, { verdict });
    expect(result.check).toHaveBeenCalledOnce();
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(result.events.some(event => Object.hasOwn(event.data, "result"))).toBe(false);
    expect(JSON.stringify([result.events, result.replies])).not.toContain(SECRET);
  });

  it.each(["mcp__foreign__", "mcp__spine__nested__"])("rejects foreign aliases %s before policy and transport", async prefix => {
    const { client, fetchImpl } = http();
    const result = await conversation(client, { plain: true, alias: `${prefix}${TOOL}` });
    expect(result.permissions).toEqual([expect.objectContaining({ behavior: "deny" })]);
    expect(result.check).not.toHaveBeenCalled();
    expect(fetchImpl).not.toHaveBeenCalled();
    expect(result.events.some(event => Object.hasOwn(event.data, "result"))).toBe(false);
  });

  it.each(["schema", "project", "revision", "digest", "extra", "source-extra", "change-extra", "document-extra", "model", "usage", "count", "order", "material",
    "index", "gallery", "gallery-title", "title", "alt", "sha", "preview", "duplicate", "suffix", "bad-document", "oversize"])(
    "rejects malformed or mismatched producer %s without UI success", async kind => {
      const value = producer();
      if (kind === "schema") value.schema = "foreign";
      if (kind === "project") value.source.project_id = TENANT;
      if (kind === "revision") value.source.revision++;
      if (kind === "digest") value.source.digest = "f".repeat(64);
      if (["extra", "model", "usage"].includes(kind)) Object.assign(value, { [kind === "extra" ? "authority_session_id" : kind]: "forged" });
      if (kind === "source-extra") Object.assign(value.source, { authority_session_id: "forged" });
      if (kind === "change-extra") Object.assign(value.changes[0]!, { url: "https://foreign.invalid" });
      if (kind === "document-extra") Object.assign(value.proposed_document, { authority: "forged" });
      if (kind === "count") value.changes.pop();
      if (kind === "order") value.changes.reverse();
      if (kind === "material") value.changes[0]!.material_id = mid(50);
      if (kind === "index") value.changes[0]!.index = 0;
      if (kind === "gallery") value.changes[0]!.gallery_slug = "bread";
      if (kind === "gallery-title") value.changes[0]!.gallery_title = "Wrong";
      if (kind === "title") value.changes[0]!.title = "Wrong";
      if (kind === "alt") value.changes[0]!.alt = "Wrong";
      if (kind === "sha") value.proposed_document.categories[0]!.images[1]!.sha256 = "f".repeat(64);
      if (kind === "preview") value.proposed_document.categories[0]!.images[1]!.preview_sha256 = "f".repeat(64);
      if (kind === "duplicate") value.proposed_document.categories[2]!.images.push(value.proposed_document.categories[0]!.images[1]!);
      if (kind === "suffix") {
        const { material_id, sha256, preview_sha256, title, alt } = operation(100);
        value.proposed_document.categories[0]!.images.push({ material_id, sha256, preview_sha256, title, alt });
      }
      if (kind === "bad-document") value.proposed_document.about = "<script>bad</script>";
      if (kind === "oversize") value.changes[0]!.material_name = "x".repeat(1024 * 1024);
      const { client } = http(value);
      const result = await conversation(client);
      expect(result.replies).toEqual([{ content: [{ type: "text", text: PHOTO_ASSIGNMENT_FAILURE }], isError: true }]);
      const output = result.events.filter(event => event.type === "tool_result");
      expect(output).toHaveLength(1);
      expect(output[0]!.data.ok).toBe(false);
      expect(output[0]!.data).not.toHaveProperty("result");
    },
  );

  it("detaches injected producer objects before durable emission", async () => {
    const value = producer();
    const appRun: AppRunClient = Object.assign(new FakeAppRunClient(), {
      proposeProjectPhotoAssignments: vi.fn(async () => value),
    });
    const result = await conversation(appRun, { plain: true });
    const output = result.emitted.find(event => event.type === "tool_result")!;
    expect(output.data.result).toEqual(value);
    expect(output.data.result).not.toBe(value);
    value.changes[0]!.title = "Mutation after dispatch";
    value.proposed_document.name = "Mutated";
    expect(output.data.result).toEqual(producer());
  });
});

describe("bounded photo assignment HTTP boundary", () => {
  const invoke = (client: HttpAppRunClient) => client.proposeProjectPhotoAssignments(TENANT, AUTHORITY.sessionId, request(), AUTHORITY);
  it.each(["declared", "actual", "status", "redirect", "redirected", "origin", "mime", "utf8", "json", "secret", "encoded-secret", "exception"])(
    "sanitizes %s and releases/cancels its response", async kind => {
      const bytes = kind === "actual" ? new Uint8Array(1024 * 1024 + 1) : kind === "utf8" ? new Uint8Array([255])
        : new TextEncoder().encode(kind === "json" ? "{" : JSON.stringify({ text:
          kind === "secret" ? SECRET : kind === "encoded-secret" ? Buffer.from(SECRET).toString("base64") : "fine" }));
      let cancelled = false;
      const response = new Response(new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes); controller.close(); },
        cancel() { cancelled = true; } }), { status: kind === "status" ? 409 : kind === "redirect" ? 302 : 200,
        headers: { "content-type": kind === "mime" ? "text/html" : "application/json",
          ...(kind === "declared" ? { "content-length": String(1024 * 1024 + 1) } : {}) } });
      if (kind === "redirected") Object.defineProperty(response, "redirected", { value: true });
      if (kind === "origin") Object.defineProperty(response, "url", { value: "https://foreign.invalid/path" });
      const fetchImpl = vi.fn<typeof fetch>().mockImplementation(async () => {
        if (kind === "exception") throw new Error(`TRANSPORT_PRIVATE ${SECRET}`);
        return response;
      });
      const client = new HttpAppRunClient({ baseUrl: "https://app.invalid", dispatchSecret: SECRET, fetchImpl });
      await expect(invoke(client)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
      expect(response.body?.locked).toBe(false);
      if (["declared", "status", "redirect", "redirected", "origin", "mime"].includes(kind)) expect(cancelled).toBe(true);
    },
  );

  it("rejects invalid input, tenant and authority before HTTP", async () => {
    const { client, fetchImpl } = http();
    await expect(client.proposeProjectPhotoAssignments(TENANT, "other", request(), AUTHORITY)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
    await expect(client.proposeProjectPhotoAssignments("bad-tenant", AUTHORITY.sessionId, request(), AUTHORITY)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
    await expect(client.proposeProjectPhotoAssignments(TENANT, AUTHORITY.sessionId,
      { ...request(), url: "https://forged.invalid" } as ReturnType<typeof request>, AUTHORITY)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it("fails closed without the dispatch secret", async () => {
    const fetchImpl = vi.fn<typeof fetch>();
    const client = new HttpAppRunClient({ baseUrl: "https://app.invalid", dispatchSecret: "", fetchImpl });
    await expect(invoke(client)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it.each(["fetch", "stream"])("enforces a total deadline on stalled %s and disposes late bodies", async kind => {
    vi.useFakeTimers();
    let cancelled = false;
    let resolve!: (response: Response) => void;
    let signal: AbortSignal | undefined;
    const response = new Response(new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } }),
      { headers: { "content-type": "application/json" } });
    const fetchImpl: typeof fetch = async (_url, init) => {
      signal = init?.signal as AbortSignal;
      return kind === "fetch" ? new Promise<Response>(done => { resolve = done; }) : response;
    };
    const client = new HttpAppRunClient({ baseUrl: "https://app.invalid", dispatchSecret: SECRET, fetchImpl });
    try {
      const rejected = expect(invoke(client)).rejects.toThrow(PHOTO_ASSIGNMENT_FAILURE);
      await vi.advanceTimersByTimeAsync(45000);
      await rejected;
      expect(signal?.aborted).toBe(true);
      if (kind === "fetch") { resolve(response); await vi.advanceTimersByTimeAsync(0); }
      expect(cancelled).toBe(true);
      expect(response.body?.locked).toBe(false);
    } finally { vi.useRealTimers(); }
  });
});

describe("photo assignment SDK catalog and private summary", () => {
  async function drain(runner: ConverseSdkRunner, input: ConverseRunInput) { for await (const _event of runner.run(input)) { /* bounded fixture */ } }
  const input = (execute: ConverseRunInput["tools"]["execute"], names: string[] = [TOOL]): ConverseRunInput => ({
    systemPrompt: "Private review", userMessage: "Review photos", tools: { list: () => names, execute },
    canUseTool: async (_name, updatedInput) => ({ behavior: "allow", updatedInput }),
  });
  it("keeps absent tools absent and denies aliases before delegating permission", async () => {
    let hook!: ConverseRunInput["canUseTool"];
    const fixture = sdkFixture(async value => { hook = value; });
    const execute = vi.fn<ConverseRunInput["tools"]["execute"]>();
    await drain(fixture.runner, input(execute, []));
    expect(fixture.mounted.some(tool => tool.name === TOOL)).toBe(false);
    expect(await hook(`mcp__spine__${TOOL}`, request())).toMatchObject({ behavior: "deny" });
    const permission = vi.fn<ConverseRunInput["canUseTool"]>().mockResolvedValue({ behavior: "deny", message: "host policy" });
    const enabled = sdkFixture(async value => { hook = value; });
    await drain(enabled.runner, { ...input(execute), canUseTool: permission });
    for (const alias of [TOOL, `mcp__foreign__${TOOL}`, `mcp__spine__nested__${TOOL}`]) {
      expect(await hook(alias, request())).toMatchObject({ behavior: "deny" });
    }
    expect(permission).not.toHaveBeenCalled();
    expect(await hook(`mcp__spine__${TOOL}`, request())).toMatchObject({ behavior: "deny", message: "host policy" });
    expect(permission).toHaveBeenCalledExactlyOnceWith(`mcp__spine__${TOOL}`, request());
    expect(execute).not.toHaveBeenCalled();
  });

  it.each(["duplicate-list", "alias-list", "duplicate-sdk"])("refuses %s catalog entries", async kind => {
    const fixture = sdkFixture();
    const execute = vi.fn<ConverseRunInput["tools"]["execute"]>();
    if (kind === "duplicate-sdk") {
      const original = fixture.sdk.tool;
      fixture.sdk.tool = (...args) => ({ ...original(...args), name: TOOL });
    }
    const names = kind === "duplicate-list" ? [TOOL, TOOL] : kind === "alias-list" ? [`mcp__foreign__${TOOL}`] : [TOOL];
    await expect(drain(fixture.runner, input(execute, names))).rejects.toThrow(/catalog|Duplicate/);
    expect(execute).not.toHaveBeenCalled();
  });

  it.each(["private-payload", "oversize", "error", "throw"])("never exposes executor %s to the model", async kind => {
    const execute = vi.fn<ConverseRunInput["tools"]["execute"]>().mockImplementation(async () => {
      if (kind === "throw") throw new Error(SECRET);
      return { content: kind === "oversize" ? SECRET.repeat(10000) : JSON.stringify(producer()),
        ...(kind === "error" ? { isError: true } : {}) };
    });
    let reply: unknown;
    const fixture = sdkFixture(async (_hook, tools) => { reply = await tools.find(tool => tool.name === TOOL)!.handler(request()); });
    await drain(fixture.runner, input(execute));
    expect(reply).toEqual({ content: [{ type: "text", text: PHOTO_ASSIGNMENT_FAILURE }], isError: true });
  });
});
