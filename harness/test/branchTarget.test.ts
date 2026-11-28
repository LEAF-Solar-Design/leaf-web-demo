import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createHarness } from "../src/server.js";
import { AuthorLoop, AuthorLoopError } from "../src/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { HarnessPorts, StagedCustomizationReceipt } from "../src/ports/index.js";
import {
  BRANCH_ACCEPTED_GRACE_MS, branchTargetPrompt, runBranchTargetSession,
  type BranchAuthorInput, type BranchAuthorSdk,
} from "../src/vendor/mushy-author/agent/branchTargetPrompt.js";
import { branchPackageDigest, submitBranchToolProposal } from "../src/vendor/mushy-author/agent/tools/submitBranchToolProposal.js";
import * as branchSubmission from "../src/vendor/mushy-author/agent/tools/submitBranchToolProposal.js";
import { decodeOperatorEntities } from "../src/vendor/mushy-author/agent/tools/decodeHtmlEntities.js";
import { branchCompileCheck, branchHostFloor, setBranchCompileCheckForTests, setBranchCompileNet48CheckForTests, type BranchCompileOutcome } from "../src/vendor/mushy-author/agent/tools/branchCompileCheck.js";
import { FsTenantRepo } from "../src/vendor/mushy-author/agent/tools/fsTenantRepo.js";
import * as repoPathSafety from "../src/vendor/mushy-author/agent/tools/repoPathSafety.js";
import {
  BranchPackageError, MAX_PICK_INPUTS, PICK_DXF_NAMES, PICK_MIN_HOST_API, pickDiagnostics, prepareBranchPackage, validateBranchPackage,
  type BranchToolProposal,
} from "../src/vendor/mushy-author/registry/branchPackageSchema.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "branch-target");
const TENANT = "branch-author-tenant";
const CHANGE = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";
const NOW = new Date("2026-11-20T00:00:00.000Z");
const ALLOWLIST_SHA = "716d548887ce792f4f9950d0ba8b0e531b3d7f9b80b7399cf3fcd59d622a3285";
const COMPILE_CONFIGURED = Boolean(process.env.LEAF_BRANCH_COMPILE_BIN && process.env.LEAF_BRANCH_COMPILE_REFS);
const dirs: string[] = [];
const servers: Server[] = [];

afterEach(async () => {
  setBranchCompileCheckForTests(null);
  setBranchCompileNet48CheckForTests(null);
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  await Promise.all(servers.splice(0).map(server => new Promise<void>((resolve, reject) => {
    server.close(error => error ? reject(error) : resolve());
    server.closeAllConnections();
  })));
  for (const dir of dirs.splice(0)) rmSync(dir, { recursive: true, force: true });
});

function temp(): string {
  const dir = mkdtempSync(join(tmpdir(), "branch-target-test-"));
  dirs.push(dir);
  return dir;
}
function fixture(name: string): BranchToolProposal {
  return JSON.parse(readFileSync(join(FIXTURES, `${name}.json`), "utf8"));
}
function filesUnder(dir: string, prefix = ""): string[] {
  return readdirSync(join(dir, prefix), { withFileTypes: true }).flatMap(entry => {
    const path = prefix ? `${prefix}/${entry.name}` : entry.name;
    return entry.isDirectory() ? filesUnder(dir, path) : [path];
  }).sort();
}
function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}
function request(base: string, id = CHANGE) {
  return {
    target: "branch" as const, changeSetId: id, expectedBaseSha: base,
    platformRelease: "sha256:test-platform", workspaceContractDigest: "a".repeat(64),
    idempotencyKey: `stage-${id}`,
  };
}

type Mounted = { name: string; handler: (args: Record<string, unknown>) => Promise<{ isError?: boolean; content: Array<{ text: string }> }> };
/** Use the real branch SDK adapter and submission handler; replace only the SDK transport. */
function sdkTransport(proposals: unknown[]) {
  const mounted: Mounted[] = [];
  const replies: Awaited<ReturnType<Mounted["handler"]>>[] = [];
  let queryOptions: Record<string, unknown> = {};
  let prompt = "";
  const close = vi.fn();
  const sdk: BranchAuthorSdk = {
    tool(name, _description, _shape, handler) { return { name, handler }; },
    createSdkMcpServer(options) { mounted.push(...options.tools as Mounted[]); return options; },
    query(input) {
      queryOptions = input.options;
      prompt = input.prompt;
      return {
        close,
        async *[Symbol.asyncIterator]() {
          const tool = mounted.find(t => t.name === "submitBranchToolProposal")!;
          for (const proposal of proposals) replies.push(await tool.handler(proposal as Record<string, unknown>));
          yield { type: "assistant", message: { model: "fixture-model", usage: { input_tokens: 12, output_tokens: 7 } } };
          yield { type: "result", subtype: "success", is_error: false, num_turns: 1 };
        },
      };
    },
  };
  const run = vi.fn((input: BranchAuthorInput) => runBranchTargetSession(input, { sdkImport: async () => sdk }));
  return { run, mounted, replies, close, options: () => queryOptions, prompt: () => prompt };
}

async function setup(proposals: unknown[]) {
  const tenantRepo = new FakeTenantRepoProvider(join(HERE, "fixtures", "tenant-repo"));
  const sdk = sdkTransport(proposals);
  const coordination = {
    recordStaged: vi.fn(async (_receipt: StagedCustomizationReceipt) => {}),
    authorizePublish: vi.fn(async () => {}),
  };
  const studio = new FakeAgentRunner();
  const studioRun = vi.spyOn(studio, "run");
  const ports: HarnessPorts = {
    tenantRepo, agentRunner: studio, oauth: new FakeOAuthGrantProvider(),
    broker: new FakeBrokerApsClient(), customizationCoordination: coordination,
  };
  const bare = await tenantRepo.bare(TENANT);
  dirs.push(dirname(bare.dir));
  const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
  return { bare, base, sdk, coordination, studioRun, ports, loop: new AuthorLoop(ports, sdk.run) };
}

describe("branch package proposals", () => {
  it("pins the harness allowlist bytes and renders that artifact in the prompt", () => {
    const bytes = readFileSync(new URL("../src/vendor/mushy-author/registry/api-allowlist.json", import.meta.url));
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(ALLOWLIST_SHA);
    const allowlist = JSON.parse(bytes.toString("utf8"));
    const prompt = branchTargetPrompt();
    for (const type of allowlist.allowed_types) expect(prompt).toContain(type);
    for (const member of allowlist.allowed_members) expect(prompt).toContain(member);
    expect(prompt).toContain("IToolContext");
    expect(prompt).toContain("panel-gap-check");
    expect(prompt).toContain("label-writer");
    expect(prompt).toContain("Branch's ScriptHost compiler against AutoCAD 2025");
    expect(prompt).toContain("V18 diagnostics");
    expect(prompt).toContain("Write C# operators literally (&&, <, >); never HTML entities.");
  });

  it("omits text style font path members from the authoring prompt", () => {
    const prompt = branchTargetPrompt();
    expect(prompt).not.toContain("TextStyleTableRecord.FileName");
    expect(prompt).not.toContain("TextStyleTableRecord.BigFontFileName");
  });

  it("includes the area-marks source verbatim and documents types and members outside the allowlist", () => {
    const prompt = branchTargetPrompt();
    expect(prompt).toContain(fixture("good-area-marks").main_csx.replace(/\n$/, ""));
    const traps = ["HatchPatternType", "HatchLoopTypes", "LineWeight", "TextHorizontalMode",
      "TextVerticalMode", "AttachmentPoint", "RXClass", "WriteMessage", "JTokenType", "Insunits", "Viewport"];
    const allowlist = JSON.parse(readFileSync(new URL("../src/vendor/mushy-author/registry/api-allowlist.json", import.meta.url), "utf8"));
    for (const name of traps) expect(prompt).toContain(name);
    for (const name of traps.filter(name => name !== "WriteMessage" && name !== "Insunits")) {
      expect(allowlist.allowed_types.some((type: string) => type.endsWith(`.${name}`))).toBe(false);
    }
    expect(prompt).toContain("After a successful submission, end the turn at once with the single word Done: no summary, no more tool calls.");
    expect(prompt).toContain('A fractional default such as 2.5 is refused. Leave it out of params and write it in main_csx: Ctx.Args.Value<double?>("x") ?? 2.5.');
    expect(prompt).toContain("defaults, minimum, maximum, multipleOf, enum and examples inside params and returns too");
    expect(prompt).toContain("ColorMethod.ByAciIndex does not exist");
    expect(prompt).toContain("Keep the outline when you fill: a fill alone does not draw the shape.");
  });

  it("documents x-leaf-pick with the corner-marks example and the pick host API", () => {
    const prompt = branchTargetPrompt();
    const corner = fixture("good-corner-marks");
    expect(corner.hosts.min_host_api).toBe(PICK_MIN_HOST_API);
    expect(prompt).toContain(corner.main_csx.replace(/\n$/, ""));
    expect(prompt).toContain('"handle": {"type": "string", "x-leaf-pick": {"prompt": "Pick a closed polyline", "dxf": ["LWPOLYLINE"], "closed": true, "implied": true}}');
    expect(prompt).toContain(`must set hosts.min_host_api to at least ${PICK_MIN_HOST_API}, the first plugin API version that supports picks.`);
    expect(prompt).toContain(`hosts: {"min_release": 2023, "min_host_api": ${PICK_MIN_HOST_API}}`);
    expect(prompt).toContain(`At most ${MAX_PICK_INPUTS} pick inputs, prompted in params.properties key order.`);
    expect(prompt).toContain("The tool never prompts itself.");
    expect(JSON.parse(prompt.split("\nparams: ")[1]!.split("\n")[0]!)).toEqual(corner.params);
  });

  it("checks x-leaf-pick declarations against the contract shape", () => {
    const pick = { prompt: "Pick a closed polyline", dxf: ["LWPOLYLINE"], closed: true, implied: true };
    const params = (properties: Record<string, unknown>) => ({ type: "object", properties });
    const ok = (p: unknown) => ({ handle: { type: "string", "x-leaf-pick": p } });
    const hosts = { min_release: 2023, min_host_api: PICK_MIN_HOST_API };
    expect(pickDiagnostics(params(ok(pick)), hosts)).toEqual([]);
    expect(pickDiagnostics(params(ok({ prompt: "x".repeat(120), dxf: PICK_DXF_NAMES.slice(0, 16) })), hosts)).toEqual([]);
    expect(pickDiagnostics(params(ok(pick)), { min_release: 2023, min_host_api: PICK_MIN_HOST_API })).toEqual([]);
    for (const hosts of [{ min_release: 2023, min_host_api: PICK_MIN_HOST_API - 1 }, {}, null]) {
      expect(pickDiagnostics(params(ok(pick)), hosts)).toContainEqual({ rule: "V10", path: "hosts",
        message: `x-leaf-pick inputs need hosts.min_host_api >= ${PICK_MIN_HOST_API}` });
    }
    expect(pickDiagnostics(params({ radius: { type: "integer" } }), {})).toEqual([]);
    expect(pickDiagnostics(params({ radius: { type: "integer" } }))).toEqual([]);
    const eight = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`p${i}`, ok(pick).handle]));
    expect(pickDiagnostics(params(eight), hosts)).toEqual([]);
    expect(pickDiagnostics(params({ ...eight, p8: ok(pick).handle }), hosts)).toEqual([
      { rule: "V7", path: "params.properties", message: "at most 8 x-leaf-pick inputs" }]);
    for (const [property, message] of [
      [{ type: "integer", "x-leaf-pick": pick }, "only valid on a \"type\": \"string\" property"],
      [{ type: ["string"], "x-leaf-pick": pick }, "only valid on a \"type\": \"string\" property"],
      [{ type: "string", "x-leaf-pick": true }, "expected an object"],
      [{ type: "string", "x-leaf-pick": { ...pick, layer: "A" } }, "unknown key layer"],
      [{ type: "string", "x-leaf-pick": { ...pick, prompt: "" } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { ...pick, prompt: "x".repeat(121) } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { ...pick, prompt: "a\u0085b" } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { dxf: ["LINE"] } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { ...pick, dxf: [] } }, "dxf must list"],
      [{ type: "string", "x-leaf-pick": { ...pick, dxf: ["lwpolyline"] } }, "dxf must list"],
      [{ type: "string", "x-leaf-pick": { ...pick, dxf: ["WIPEOUT"] } }, "dxf must list"],
      [{ type: "string", "x-leaf-pick": { ...pick, dxf: ["LINE", "LINE"] } }, "dxf must list"],
      [{ type: "string", "x-leaf-pick": { ...pick, prompt: "   " } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { ...pick, prompt: "a" + String.fromCodePoint(0x200b) + "b" } }, "prompt must be"],
      [{ type: "string", "x-leaf-pick": { ...pick, dxf: PICK_DXF_NAMES.slice(0, 17) } }, "dxf must list"],
      [{ type: "string", "x-leaf-pick": { ...pick, closed: "true" } }, "closed must be a boolean"],
      [{ type: "string", "x-leaf-pick": { ...pick, implied: 1 } }, "implied must be a boolean"],
    ] as Array<[Record<string, unknown>, string]>) {
      const diagnostics = pickDiagnostics(params({ handle: property }));
      expect(diagnostics, JSON.stringify(property)).toContainEqual(expect.objectContaining({
        rule: "V7", path: "params.properties.handle.x-leaf-pick", message: expect.stringContaining(message) }));
    }
    const corner = fixture("good-corner-marks");
    const bad = { ...corner, params: { ...corner.params, properties: { handle: { type: "integer", "x-leaf-pick": pick } } } };
    expect(() => prepareBranchPackage(bad, "pick-test", NOW)).toThrow("V7: params.properties.handle.x-leaf-pick:");
  });

  // One real-compiler case (allowlisted as the single skip in run-all-gates): area-marks, its HTML-entity
  // repair, and the corner-marks pick example the author prompt teaches.
  it.skipIf(!COMPILE_CONFIGURED)("compiles area-marks and corner-marks, and refuses HTML-encoded operators", async () => {
    const corner = fixture("good-corner-marks");
    const cornerPackage = prepareBranchPackage(corner, "compile-test", NOW);
    expect((await branchCompileCheck(corner.name, cornerPackage.files)).status).toBe("accept");
    const proposal = fixture("good-area-marks");
    const good = prepareBranchPackage(proposal, "compile-test", NOW);
    expect((await branchCompileCheck(proposal.name, good.files)).status).toBe("accept");
    const source = proposal.main_csx.replace("pl.NumberOfVertices < 3", "pl.NumberOfVertices &lt; 3")
      .replace("marked > 0", "marked &gt; 0");
    expect(source).not.toBe(proposal.main_csx);
    const encoded = prepareBranchPackage({ ...proposal, main_csx: source }, "compile-test", NOW);
    expect((await branchCompileCheck(proposal.name, encoded.files)).status).toBe("refused");
    const decoded = decodeOperatorEntities(source);
    expect(decoded).toEqual({ source: proposal.main_csx, entities: 2 });
    const repaired = prepareBranchPackage({ ...proposal, main_csx: decoded!.source }, "compile-test", NOW);
    expect((await branchCompileCheck(proposal.name, repaired.files)).status).toBe("accept");
  }, 60_000);

  it.each(["good-panel-count", "good-label-writer", "good-area-marks", "good-corner-marks"])("writes exactly the recorded %s package", name => {
    const root = temp();
    const proposal = fixture(name);
    const submitted = submitBranchToolProposal(root, proposal, { session: "fixture-session", now: NOW });
    const expected = [`branch-tools/${proposal.name}/tool.json`, `branch-tools/${proposal.name}/main.csx`,
      ...Object.keys(proposal.lib).map(n => `branch-tools/${proposal.name}/lib/${n}`)].sort();
    expect(submitted.files).toEqual(expected);
    expect(filesUnder(root)).toEqual(expected);
    expect(filesUnder(root).some(p => p.endsWith("tool.py"))).toBe(false);
    expect(existsSync(join(root, "registry.json"))).toBe(false);
    const manifest = JSON.parse(readFileSync(join(root, submitted.package_path, "tool.json"), "utf8"));
    const { main_csx, lib, ...metadata } = proposal;
    expect(manifest).toEqual({ ...metadata, entry: "main.csx",
      provenance: { author: "agent", session: "fixture-session", created: NOW.toISOString(), modified: NOW.toISOString() },
      review: { status: "unreviewed" } });
    expect(readFileSync(join(root, submitted.package_path, "main.csx"), "utf8")).toBe(main_csx);
    for (const [path, content] of Object.entries(lib)) expect(readFileSync(join(root, submitted.package_path, "lib", path), "utf8")).toBe(content);
    expect(validateBranchPackage(manifest, submitted.contents, proposal.name)).toEqual([]);
    const digestInput = Object.keys(submitted.contents).sort().map(path => {
      const bytes = Buffer.from(submitted.contents[path]);
      return `${path}\t${bytes.length}\t${createHash("sha256").update(bytes).digest("hex")}\n`;
    }).join("");
    expect(submitted.package_digest).toBe(createHash("sha256").update(digestInput).digest("hex"));
  });

  it.each([
    ["bad-unknown-key", "V2"], ["bad-r-directive", "V17"],
    ["bad-net-http", "V8"], ["bad-65-files", "V16"],
  ])("refuses recorded %s with %s and writes nothing", (name, rule) => {
    const root = temp();
    try {
      submitBranchToolProposal(root, fixture(name));
      expect.fail("invalid recorded proposal was accepted");
    } catch (error) {
      expect(error).toBeInstanceOf(BranchPackageError);
      expect((error as BranchPackageError).diagnostics.some(d => d.rule === rule)).toBe(true);
    }
    expect(readdirSync(root)).toEqual([]);
  });

  it("counts both mandatory files at the 64-file boundary", () => {
    const root = temp();
    const proposal = fixture("good-label-writer");
    proposal.lib = Object.fromEntries(Array.from({ length: 62 }, (_, i) => [`helper-${i}.csx`, "// helper\n"]));
    expect(submitBranchToolProposal(root, proposal).files).toHaveLength(64);
  });

  it.each([
    ["bad-unknown-key", "V2"], ["bad-r-directive", "V17"],
    ["bad-net-http", "V8"], ["bad-65-files", "V16"],
  ])("refuses recorded %s through the installed SDK's MCP transport with %s", async (name, rule) => {
    const root = temp();
    const installed = await import(["@anthropic-ai", "claude-agent-sdk"].join("/")) as BranchAuthorSdk;
    const client = new Client({ name: "branch-proposal-test", version: "1.0.0" });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let mcp: McpServer | undefined;
    let response: Awaited<ReturnType<Client["callTool"]>> | undefined;
    const sdk: BranchAuthorSdk = {
      tool: installed.tool,
      createSdkMcpServer(options) {
        const config = installed.createSdkMcpServer(options) as { instance: McpServer };
        mcp = config.instance;
        return config;
      },
      query() {
        return {
          close() {},
          async *[Symbol.asyncIterator]() {
            await mcp!.connect(serverTransport);
            await client.connect(clientTransport);
            response = await client.callTool({ name: "submitBranchToolProposal", arguments: fixture(name) as unknown as Record<string, unknown> });
            yield { type: "result", subtype: "success", is_error: false };
          },
        };
      },
    };
    try {
      await expect(runBranchTargetSession({
        description: "recorded invalid proposal", systemPrompt: branchTargetPrompt(),
        repoDir: root, grant: { kind: "oauth", oauthToken: "fixture-branch-grant" },
        fsTenantRepo: new FsTenantRepo(root),
        submitBranchToolProposal: proposal => submitBranchToolProposal(root, proposal),
      }, { sdkImport: async () => sdk })).rejects.toThrow("without a validated package");
      expect(response?.isError).toBe(true);
      expect(response?.content).toEqual(expect.arrayContaining([
        expect.objectContaining({ type: "text", text: expect.stringContaining(`${rule}:`) }),
      ]));
      expect(readdirSync(root)).toEqual([]);
    } finally {
      await client.close();
      await mcp?.close();
    }
  });

  it("preserves source bytes through installed SDK MCP, submission, compile check and staged Git tree", async () => {
    vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", undefined);
    const source = 'int a = 1, b = 2, c = 4, d = 3;\nif (a < b && c >= d) { }\nstring entities = "&lt;&amp;&quot;";\nreturn entities;\n';
    const proposal = { ...fixture("good-label-writer"), main_csx: source,
      lib: { "literal.csx": '// π and literal entities\nstring literal = "&lt;&amp;&quot;";\n' } };
    const { bare, base, ports } = await setup([]);
    const compiled: Array<{ name: string; files: Record<string, string> }> = [];
    setBranchCompileCheckForTests(async (name, files) => {
      compiled.push({ name, files: { ...files } });
      return { status: "accept", ms: 0, allowlistSha256: ALLOWLIST_SHA,
        summary: { pass: true, decision: "accept", errors: 0 } };
    });
    const installed = await import(["@anthropic-ai", "claude-agent-sdk"].join("/")) as BranchAuthorSdk;
    const client = new Client({ name: "branch-source-fidelity-test", version: "1.0.0" });
    const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
    let mcp: McpServer | undefined;
    let response: Awaited<ReturnType<Client["callTool"]>> | undefined;
    const sdk: BranchAuthorSdk = {
      tool: installed.tool,
      createSdkMcpServer(options) {
        const config = installed.createSdkMcpServer(options) as { instance: McpServer };
        mcp = config.instance;
        return config;
      },
      query() {
        return {
          close() {},
          async *[Symbol.asyncIterator]() {
            await mcp!.connect(serverTransport);
            await client.connect(clientTransport);
            response = await client.callTool({ name: "submitBranchToolProposal",
              arguments: proposal as unknown as Record<string, unknown> });
            yield { type: "result", subtype: "success", is_error: false };
          },
        };
      },
    };
    const received: unknown[] = [];
    const written: Record<string, string>[] = [];
    const loop = new AuthorLoop(ports, input => runBranchTargetSession({
      ...input,
      submitBranchToolProposal: async value => {
        received.push(value);
        const result = await input.submitBranchToolProposal(value);
        written.push(Object.fromEntries(Object.keys(result.contents).map(path => [path,
          readFileSync(join(input.repoDir, result.package_path, path), "utf8")])));
        return result;
      },
    }, { sdkImport: async () => sdk }));
    try {
      const { receipt } = await loop.stage(TENANT, "preserve literal source", request(base));
      expect(response?.isError).not.toBe(true);
      expect(received).toEqual([proposal]);
      expect(compiled).toHaveLength(1);
      expect(compiled[0].name).toBe(proposal.name);
      expect(written).toEqual([compiled[0].files]);
      const sources = { "main.csx": source, "lib/literal.csx": proposal.lib["literal.csx"] };
      for (const [path, text] of Object.entries(sources)) {
        expect(Buffer.from(compiled[0].files[path], "utf8")).toEqual(Buffer.from(text, "utf8"));
      }
      expect(receipt.package_files).toEqual(Object.keys(compiled[0].files)
        .map(path => `${receipt.package_path}/${path}`).sort());
      for (const [path, text] of Object.entries(compiled[0].files)) {
        // Read blobs directly: the git helper trims whitespace and would mask byte changes.
        const blob = execFileSync("git", ["show", `${receipt.staged_commit}:${receipt.package_path}/${path}`], { cwd: bare.dir });
        expect(blob).toEqual(Buffer.from(text, "utf8"));
      }
    } finally {
      await client.close();
      await mcp?.close();
    }
  });

  it.each<[string, Record<string, unknown>]>([
    ["V3", { name: "panel-count\n" }], ["V4", { version: "01.0.0" }],
    ["V5", { description: "hidden\u202econtrol" }], ["V7", { params: { type: "object", required: "bad" } }],
    ["V7", { returns: { properties: { count: { type: "invalid" } } } }],
    ["V8", { capabilities: ["files.read:*"] }], ["V10", { hosts: { min_release: 2023, min_host_api: 0 } }],
    ["V11", { timeout_ms: 600001 }], ["V12", { idempotent: "true" }],
    ["V15", { params: { type: "object", properties: { x: { type: "number", minimum: 0.5 } } } }],
    ["V16", { lib: { "../escape.csx": "return 1;" } }],
    ["V17", { main_csx: '#load "lib/missing.csx"\nreturn 1;' }],
    ["V17", { main_csx: '# load "lib/missing.csx"' }],
    ["V17", { main_csx: '\ufeffreturn 1;' }],
    ["V2", { entry: "tool.py" }], ["V2", { provenance: { author: "human" } }],
    ["V2", { review: { status: "reviewed" } }],
  ])("reports %s before any filesystem mutation", (rule, patch) => {
    const root = temp();
    expect(() => submitBranchToolProposal(root, { ...fixture("good-label-writer"), ...patch })).toThrow(`${rule}:`);
    expect(readdirSync(root)).toEqual([]);
  });

  it("measures the 4 MiB limit in UTF-8 bytes", () => {
    const root = temp();
    const proposal = { ...fixture("good-label-writer"), main_csx: "é".repeat(2 * 1024 * 1024) };
    expect(() => submitBranchToolProposal(root, proposal)).toThrow("V16:");
    expect(readdirSync(root)).toEqual([]);
  });

  it("does not overwrite an existing package", () => {
    const root = temp();
    const proposal = fixture("good-label-writer");
    const first = submitBranchToolProposal(root, proposal, { now: NOW });
    expect(() => submitBranchToolProposal(root, proposal)).toThrow("V3:");
    for (const [path, bytes] of Object.entries(first.contents)) expect(readFileSync(join(root, first.package_path, path), "utf8")).toBe(bytes);
    expect(filesUnder(root)).toEqual(first.files);
  });

  it("keeps directory link counts stable until revision publication on every platform", () => {
    const root = temp();
    const proposal = fixture("good-label-writer");
    submitBranchToolProposal(root, proposal, { now: NOW });
    const resolvePath = repoPathSafety.resolveContainedRepoPath;
    const revalidatePath = repoPathSafety.revalidateResolvedRepoPath;
    const snapshots = new WeakMap<repoPathSafety.ResolvedRepoPath, number[]>();
    // Counting child directories models Linux directory nlink changes even
    // where the filesystem reports a constant nlink (for example Windows).
    const childDirectories = (snapshot: repoPathSafety.ResolvedRepoPath) => snapshot.components
      .filter(component => component.kind === "directory")
      .map(component => readdirSync(component.path, { withFileTypes: true }).filter(entry => entry.isDirectory()).length);
    vi.spyOn(repoPathSafety, "resolveContainedRepoPath").mockImplementation((...args) => {
      const snapshot = resolvePath(...args);
      if (snapshot) snapshots.set(snapshot, childDirectories(snapshot));
      return snapshot;
    });
    const revalidate = vi.spyOn(repoPathSafety, "revalidateResolvedRepoPath").mockImplementation(snapshot => {
      expect(childDirectories(snapshot)).toEqual(snapshots.get(snapshot));
      const current = revalidatePath(snapshot);
      snapshots.set(current, childDirectories(current));
      return current;
    });
    const options = { targetToolName: proposal.name, now: NOW };
    const added = submitBranchToolProposal(root, {
      ...proposal, version: "1.1.0", lib: { "helper.csx": "// helper\n" },
    }, options);
    expect(filesUnder(root)).toEqual(added.files);
    expect(existsSync(join(root, added.package_path, "lib", "helper.csx"))).toBe(true);
    const removed = submitBranchToolProposal(root, { ...proposal, version: "1.2.0" }, options);
    expect(filesUnder(root)).toEqual(removed.files);
    expect(existsSync(join(root, removed.package_path, "lib"))).toBe(false);
    expect(revalidate).toHaveBeenCalled();
  });

  it.skipIf(process.platform === "win32")("refuses a linked package parent", () => {
    const root = temp();
    const outside = temp();
    symlinkSync(outside, join(root, "branch-tools"), "dir");
    expect(() => submitBranchToolProposal(root, fixture("good-label-writer"))).toThrow("V16:");
    expect(readdirSync(outside)).toEqual([]);
  });

  it("checks generated-manifest fields using their contract rule ids", () => {
    const prepared = prepareBranchPackage(fixture("good-label-writer"), "test", NOW);
    for (const [rule, patch] of [
      ["V6", { entry: "tool.py" }], ["V9", { family: "bad/family" }],
      ["V13", { provenance: { author: "human" } }], ["V14", { review: { status: "reviewed" } }],
    ] as const) {
      const manifest = { ...prepared.manifest, ...patch };
      expect(validateBranchPackage(manifest, { ...prepared.files, "tool.json": JSON.stringify(manifest) }, prepared.manifest.name).some(d => d.rule === rule)).toBe(true);
    }
  });
});

describe("branch stage lifecycle", () => {
  beforeEach(() => { vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", "off"); });
  it.each(["good-panel-count", "good-label-writer", "good-area-marks"])("stages %s through the branch SDK tool without registering Python", async name => {
    const proposal = fixture(name);
    const { bare, base, sdk, coordination, studioRun, loop } = await setup([proposal]);
    const staged = await loop.stage(TENANT, "author this C# package", request(base));
    const receipt = staged.receipt;
    const expectedFiles = [`branch-tools/${proposal.name}/tool.json`, `branch-tools/${proposal.name}/main.csx`,
      ...Object.keys(proposal.lib).map(n => `branch-tools/${proposal.name}/lib/${n}`)].sort();
    expect(receipt).toEqual({
      contract: "leaf.customization.v1", tenant_id: TENANT, change_set_id: CHANGE, state: "staged",
      base_commit: base, staged_commit: expect.any(String),
      catalog_digest: createHash("sha256").update(execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir })).digest("hex"),
      platform_release: "sha256:test-platform", workspace_contract_digest: "a".repeat(64), idempotency_key: `stage-${CHANGE}`,
      target: "branch", package_path: `branch-tools/${proposal.name}`, package_files: expectedFiles,
    });
    expect(receipt.staged_commit).not.toBe(base);
    expect(git(bare.dir, ["diff", "--name-only", base, receipt.staged_commit]).split("\n").sort()).toEqual(expectedFiles);
    expect(git(bare.dir, ["show", `${receipt.staged_commit}:registry.json`])).toBe(git(bare.dir, ["show", `${base}:registry.json`]));
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(receipt.staged_commit);
    expect(studioRun).not.toHaveBeenCalled();
    expect(sdk.replies).toHaveLength(1);
    expect(sdk.replies[0].isError).toBeUndefined();
    expect(sdk.mounted.map(t => t.name)).toEqual(["fs_tenant_repo", "submitBranchToolProposal"]);
    expect(sdk.options().settingSources).toEqual([]);
    expect(sdk.options().tools).toEqual([]);
    expect(sdk.options().allowedTools).toEqual([
      "mcp__branch_author__fs_tenant_repo", "mcp__branch_author__submitBranchToolProposal",
    ]);
    const permission = sdk.options().canUseTool as (name: string, args: Record<string, unknown>) => Promise<{ behavior: string }>;
    expect(await permission("Bash", {})).toMatchObject({ behavior: "deny" });
    expect(await permission("unknown_tool", {})).toMatchObject({ behavior: "deny" });
    expect(sdk.prompt()).toContain("IToolContext");
    expect(sdk.close).toHaveBeenCalledOnce();
    expect(coordination.recordStaged).toHaveBeenCalledWith(receipt);
    expect((await loop.stage(TENANT, "retry", request(base))).receipt).toEqual(receipt);
    expect(sdk.run).toHaveBeenCalledOnce();
  });

  it("recovers the exact branch receipt after durable coordination fails", async () => {
    const { base, sdk, coordination, loop } = await setup([fixture("good-panel-count")]);
    coordination.recordStaged.mockRejectedValueOnce(new Error("coordination unavailable"));
    await expect(loop.stage(TENANT, "count panels", request(base))).rejects.toThrow("coordination unavailable");
    const firstReceipt = coordination.recordStaged.mock.calls[0]?.[0];
    const resumed = await loop.stage(TENANT, "retry after interruption", request(base));
    expect(resumed.receipt).toEqual(firstReceipt);
    expect(resumed.receipt.target).toBe("branch");
    expect(sdk.run).toHaveBeenCalledOnce();
  });

  it.each([undefined, "studio"] as const)("keeps the Studio receipt key set for target %s", async target => {
    const { base, sdk, studioRun, loop } = await setup([]);
    const { target: _target, ...options } = request(base);
    const staged = await loop.stage(TENANT, "count entities per layer", {
      ...options, ...(target === undefined ? {} : { target }),
    });
    expect(Object.keys(staged.receipt).sort()).toEqual([
      "contract", "tenant_id", "change_set_id", "state", "base_commit", "staged_commit",
      "catalog_digest", "platform_release", "workspace_contract_digest", "idempotency_key",
    ].sort());
    expect(staged.tool?.entry).toMatch(/^tools\/[^/]+\/tool\.py$/);
    expect(studioRun).toHaveBeenCalledOnce();
    expect(sdk.run).not.toHaveBeenCalled();
  });

  it("does not relabel a pending branch change as Studio on retry", async () => {
    const { base, loop } = await setup([fixture("good-label-writer")]);
    await loop.stage(TENANT, "label", request(base));
    await expect(loop.stage(TENANT, "retry with a different target", {
      ...request(base), target: "studio",
    })).rejects.toThrow("cannot be resumed as studio");
  });

  it("returns validation feedback, repairs a bad proposal, and commits only the good package", async () => {
    const { base, sdk, loop } = await setup([fixture("bad-r-directive"), fixture("good-label-writer")]);
    const staged = await loop.stage(TENANT, "write label", request(base));
    expect(sdk.replies[0].isError).toBe(true);
    expect(sdk.replies[0].content[0].text).toContain("V17:");
    expect(sdk.replies[1].isError).toBeUndefined();
    expect(staged.receipt.package_path).toBe("branch-tools/label-writer");
  });

  it("refuses a session with no accepted proposal without making a staged commit", async () => {
    const { bare, base, sdk, coordination, studioRun, loop } = await setup([fixture("bad-net-http")]);
    await expect(loop.stage(TENANT, "invalid network tool", request(base))).rejects.toThrow("without a validated package");
    expect(sdk.replies[0].content[0].text).toContain("V8:");
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(base);
    expect(coordination.recordStaged).not.toHaveBeenCalled();
    expect(studioRun).not.toHaveBeenCalled();
  });

  it("rejects a runner's unrelated edit before committing", async () => {
    const { ports, bare, base } = await setup([]);
    const loop = new AuthorLoop(ports, async input => {
      await input.submitBranchToolProposal(fixture("good-label-writer"));
      writeFileSync(join(input.repoDir, "unrelated.txt"), "must not enter the commit");
      return { preview: "test" };
    });
    await expect(loop.stage(TENANT, "label", request(base))).rejects.toThrow("outside the submitted branch package");
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(base);
  });

  it("revises a branch package with a higher version without changing the catalog", async () => {
    const firstProposal = fixture("good-label-writer");
    const { bare, base, ports, loop } = await setup([firstProposal]);
    const first = await loop.stage(TENANT, "label", request(base));
    await loop.publish(first.receipt, base);
    const proposal = { ...firstProposal, version: "1.1.0", main_csx: `${firstProposal.main_csx}\n// revised\n` };
    const transport = sdkTransport([proposal]);
    const revisedLoop = new AuthorLoop(ports, transport.run);
    const revised = await revisedLoop.stage(TENANT, "revise label", {
      ...request(first.receipt.staged_commit, SECOND), targetToolName: firstProposal.name,
    });
    expect(revised.receipt.catalog_digest).toBe(first.receipt.catalog_digest);
    expect(git(bare.dir, ["diff", "--name-only", first.receipt.staged_commit, revised.receipt.staged_commit]).split("\n").every(p => p.startsWith("branch-tools/label-writer/"))).toBe(true);
    const bad = new AuthorLoop(ports, sdkTransport([firstProposal]).run);
    await expect(bad.stage(TENANT, "same version", {
      ...request(first.receipt.staged_commit, "33333333-3333-4333-8333-333333333333"), targetToolName: firstProposal.name,
    })).rejects.toThrow("without a validated package");
  });

  it("uses the selected tenant's grant lease and settles branch usage", async () => {
    const { base, ports } = await setup([]);
    const acquireGrant = vi.fn(async () => ({
      account_id: "branch-account", lease_id: "branch-lease",
      grant: { kind: "oauth" as const, oauthToken: "fixture-branch-grant" },
    }));
    const settleGrant = vi.fn(async () => {});
    const loop = new AuthorLoop({ ...ports, oauth: { getGrant: vi.fn(), acquireGrant, settleGrant } }, async input => {
      expect(input.grant).toEqual({ kind: "oauth", oauthToken: "fixture-branch-grant" });
      await input.submitBranchToolProposal(fixture("good-panel-count"));
      return { preview: "test", telemetry: { input_tokens: 17, output_tokens: 3 } };
    });
    await loop.stage(TENANT, "panels", request(base));
    expect(acquireGrant).toHaveBeenCalledWith(TENANT);
    expect(settleGrant).toHaveBeenCalledWith(TENANT, "branch-lease", { usage: { cost_tokens: 20 }, stop_reason: "end_turn" });
  });

  it.each(["1.1.0", "1.0.0"])("revises a published tree off main with proposal version %s", async version => {
    const proposal = fixture("good-label-writer");
    const { base, bare, loop, ports } = await setup([proposal]);
    const first = await loop.stage(TENANT, "create label writer", request(base));
    const sha = first.receipt.staged_commit;
    const pkg = "branch-tools/label-writer";
    const transport = sdkTransport([{ ...proposal, version }]);
    const revisedLoop = new AuthorLoop(ports, async input => {
      expect(input.fsTenantRepo.exists(pkg + "/tool.json")).toBe(true);
      expect(input.fsTenantRepo.readFile(pkg + "/main.csx")).toBe(proposal.main_csx);
      expect(input.systemPrompt).toContain("This is a revision of branch-tools/label-writer (current version 1.0.0)");
      return transport.run(input);
    });
    const revision = revisedLoop.stage(TENANT, "revise label writer", {
      ...request(base, SECOND), targetToolName: proposal.name, revisionBaseCommit: sha,
    });
    if (version === "1.0.0") {
      await expect(revision).rejects.toMatchObject({ status: 422,
        reason: { code: "branch_author_package_refused", rule: "V4", turns: 1, submit_calls: 1 } });
      expect(git(bare.dir, ["rev-parse", "refs/leaf/changes/" + SECOND])).toBe(base);
      return;
    }
    const staged = (await revision).receipt.staged_commit;
    const seed = git(bare.dir, ["rev-parse", staged + "^"]);
    expect(git(bare.dir, ["rev-parse", seed + "^"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", seed + ":" + pkg])).toBe(git(bare.dir, ["rev-parse", sha + ":" + pkg]));
    expect(git(bare.dir, ["diff", "--name-only", base, staged]).split("\n").every(path => path.startsWith(pkg + "/"))).toBe(true);
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });

  it.each(["absent", "unresolvable", "no-package"])("refuses a revision with %s base before authoring", async mode => {
    const { base, bare, sdk, loop, ports } = await setup([]);
    const grant = vi.spyOn(ports.oauth, "getGrant");
    await expect(loop.stage(TENANT, "revise missing label writer", {
      ...request(base), targetToolName: "label-writer",
      ...(mode === "absent" ? {} : { revisionBaseCommit: mode === "unresolvable" ? "0".repeat(40) : base }),
    })).rejects.toMatchObject({ status: 422, reason: { code: "branch_revision_target_missing" } });
    expect(sdk.run).not.toHaveBeenCalled();
    expect(grant).not.toHaveBeenCalled();
    expect(git(bare.dir, ["rev-parse", "refs/leaf/changes/" + CHANGE])).toBe(base);
  });

  it("reports the counted assistant turns when the session ends without a proposal", async () => {
    const { base, loop } = await setup([]);
    await expect(loop.stage(TENANT, "label", request(base))).rejects.toMatchObject({
      status: 422, reason: { code: "branch_author_no_submission", turns: 1, submit_calls: 0 },
    });
  });
});

describe("D6 net48 host floor", () => {
  const accepted: BranchCompileOutcome = {
    status: "accept", ms: 1, allowlistSha256: ALLOWLIST_SHA,
    summary: { pass: true, decision: "accept", errors: 0 },
  };
  const refused: BranchCompileOutcome = {
    status: "refused", decision: "compile_failed", ms: 4,
    diagnostics: ["main.csx(2,3): CS1061: missing MaxBy"],
    structured: [{ file: "main.csx", line: 2, column: 3, code: "CS1061", message: "missing MaxBy" }],
    summary: { pass: false, decision: "compile_failed", errors: 1 },
  };
  beforeEach(() => {
    vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", undefined);
    vi.stubEnv("LEAF_BRANCH_COMPILE_NET48_BIN", undefined);
    setBranchCompileCheckForTests(async () => accepted);
  });

  it.each([
    ["refused", refused, 2023, 2025],
    ["accept", accepted, 2023, 2023],
    ["timeout", { status: "unavailable", code: "timeout", ms: 4,
      summary: { pass: false, decision: "unavailable", errors: 0 } } as BranchCompileOutcome, 2023, 2025],
    ["not_configured", { status: "unavailable", code: "not_configured", ms: 0,
      summary: { pass: false, decision: "unavailable", errors: 0 } } as BranchCompileOutcome, 2023, 2023],
    ["higher declared", refused, 2026, 2026],
  ] as const)("labels %s and binds the staged digest", async (_name, outcome, declared, expected) => {
    const proposal = fixture("good-label-writer");
    proposal.hosts.min_release = declared;
    const check48 = vi.fn(async () => outcome);
    setBranchCompileNet48CheckForTests(check48);
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const { base, bare, ports } = await setup([]);
    let submitted: ReturnType<typeof submitBranchToolProposal> | undefined;
    const loop = new AuthorLoop(ports, async input => {
      submitted = await input.submitBranchToolProposal(proposal);
      return { preview: "Done" };
    });
    const result = await loop.stage(TENANT, "write label", request(base));
    const manifest = JSON.parse(git(bare.dir, ["show", result.receipt.staged_commit + ":branch-tools/label-writer/tool.json"]));
    expect(manifest.hosts.min_release).toBe(expected);
    expect(JSON.parse(submitted!.contents["tool.json"]).hosts.min_release).toBe(expected);
    expect(submitted!.package_digest).toBe(branchPackageDigest(submitted!.contents));
    expect(check48).toHaveBeenCalledTimes(1);
    expect(check48).toHaveBeenCalledWith("label-writer", expect.any(Object), { target: "net48" });
    const decision = outcome.status === "unavailable" ? `unavailable:${outcome.code}` : outcome.summary.decision;
    expect(log).toHaveBeenCalledWith(`[harness] branch net48 check: decision=${decision} ms=${outcome.ms} min_release=${expected} tenant=${TENANT}`);
    expect(result.compile_attempts).toHaveLength(1);
  });

  it("preserves V18 on net8 refusal and never runs net48", async () => {
    setBranchCompileCheckForTests(async () => refused);
    const check48 = vi.fn(async () => accepted);
    setBranchCompileNet48CheckForTests(check48);
    const { base, loop, sdk } = await setup([fixture("good-label-writer")]);
    await expect(loop.stage(TENANT, "write label", request(base))).rejects.toMatchObject({
      status: 422, reason: { rule: "V18" },
    });
    expect(sdk.replies[0].content[0].text).toContain("V18:");
    expect(check48).not.toHaveBeenCalled();
  });

  it.each(["disabled", "unavailable"])("does not run net48 when net8 is %s", async mode => {
    const check48 = vi.fn(async () => accepted);
    setBranchCompileNet48CheckForTests(check48);
    const { base, loop } = await setup([fixture("good-label-writer")]);
    if (mode === "disabled") {
      vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", "off");
      await loop.stage(TENANT, "write label", request(base));
    } else {
      setBranchCompileCheckForTests(async () => ({ status: "unavailable", code: "timeout", ms: 1,
        summary: { pass: false, decision: "unavailable", errors: 0 } }));
      await expect(loop.stage(TENANT, "write label", request(base))).rejects.toMatchObject({ status: 422 });
    }
    expect(check48).not.toHaveBeenCalled();
  });

  it("keeps a thrown net48 check from failing a submission", async () => {
    setBranchCompileNet48CheckForTests(async () => { throw new Error("checker failed"); });
    const { base, bare, loop } = await setup([fixture("good-label-writer")]);
    const result = await loop.stage(TENANT, "write label", request(base));
    expect(JSON.parse(git(bare.dir, ["show", result.receipt.staged_commit + ":branch-tools/label-writer/tool.json"]))
      .hosts.min_release).toBe(2025);
  });

  it("uses the snapshotted hosts when the caller mutates its proposal during a check", async () => {
    const proposal = fixture("good-label-writer");
    setBranchCompileNet48CheckForTests(async () => {
      proposal.hosts.min_release = 2026;
      return refused;
    });
    const { base, bare, loop } = await setup([proposal]);
    const result = await loop.stage(TENANT, "write label", request(base));
    expect(JSON.parse(git(bare.dir, ["show", result.receipt.staged_commit + ":branch-tools/label-writer/tool.json"]))
      .hosts.min_release).toBe(2025);
  });

  it.each(["busy", "spawn_failed", "bad_output"] as const)("fails safe for unavailable %s", code => {
    expect(branchHostFloor({ status: "unavailable", code, ms: 1,
      summary: { pass: false, decision: "unavailable", errors: 0 } })).toBe(2025);
  });
});

describe("Branch submit compile gate", () => {
  beforeEach(() => {
    vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", undefined);
    vi.stubEnv("LEAF_BRANCH_COMPILE_BIN", undefined);
    vi.stubEnv("LEAF_BRANCH_COMPILE_REFS", undefined);
  });
  const refusal: Extract<BranchCompileOutcome, { status: "refused" }> = {
    status: "refused", decision: "compile_failed", ms: 4,
    summary: { pass: false, decision: "compile_failed", errors: 2 },
    diagnostics: ["main.csx(2,3): CS0117: unknown member", "lib/a.csx(4,5): scan: denied namespace: System.IO"],
    structured: [
      { file: "main.csx", line: 2, column: 3, code: "CS0117", message: "unknown member" },
      { file: "lib/a.csx", line: 4, column: 5, code: "scan", message: "denied namespace: System.IO" },
    ],
  };
  const reason = { code: "branch_author_package_refused", rule: "V18", turns: 1, submit_calls: 1 };
  const refusedAttempt = {
    n: 1, decision: "compile_failed", errors: 2,
    diagnostics: [
      { code: "CS0117", file: "main.csx", line: 2 },
      { code: "scan", file: "lib/a.csx", line: 4 },
    ],
  };

  const parserRefusal: Extract<BranchCompileOutcome, { status: "refused" }> = {
    status: "refused", decision: "compile_failed", ms: 1,
    summary: { pass: false, decision: "compile_failed", errors: 1 },
    diagnostics: ["main.csx(1,8): CS1525: invalid expression"],
    structured: [{ file: "main.csx", line: 1, column: 8, code: "CS1525", message: "invalid expression" }],
  };
  const accepted: BranchCompileOutcome = {
    status: "accept", ms: 1, allowlistSha256: ALLOWLIST_SHA,
    summary: { pass: true, decision: "accept", errors: 0 },
  };
  const parserAttempt = { n: 1, decision: "compile_failed", errors: 1,
    diagnostics: [{ file: "main.csx", line: 1, code: "CS1525" }] };
  const originalMessage = "V18: branch-tools/label-writer/main.csx: compile failed: " +
    parserRefusal.diagnostics[0] + " (source contains HTML entities; write C# operators such as && < > literally)";

  it("binds the decoded source to compiler input, disk, digest and staged bytes with one disclosed attempt", async () => {
    const source = "if (1 &lt; 2 &amp;&amp; 3 &gt;= 2) { }\nreturn 1;\n";
    const proposal = { ...fixture("good-label-writer"), main_csx: source };
    const decoded = decodeOperatorEntities(source)!;
    const compile = vi.fn(async (_name: string, files: Record<string, string>) =>
      files["main.csx"] === source ? parserRefusal : accepted);
    setBranchCompileCheckForTests(compile);
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const { base, bare, ports } = await setup([]);
    let submitted: ReturnType<typeof submitBranchToolProposal> | undefined;
    const sdk = sdkTransport([proposal]);
    // Reuse the transport's adapter to capture replies while observing publication.
    const observed = new AuthorLoop(ports, async input => {
      const submit = input.submitBranchToolProposal;
      return sdk.run({ ...input, submitBranchToolProposal: async value => {
        submitted = await submit(value);
        for (const [path, text] of Object.entries(submitted.contents)) {
          expect(readFileSync(join(input.repoDir, submitted.package_path, path), "utf8")).toBe(text);
        }
        return submitted;
      } });
    });
    const result = await observed.stage(TENANT, "decode source", request(base));
    expect(proposal.main_csx).toBe(source);
    expect(compile).toHaveBeenCalledTimes(2);
    expect(compile.mock.calls[0][1]["main.csx"]).toBe(source);
    expect(compile.mock.calls[1][1]["main.csx"]).toBe(decoded.source);
    expect(submitted!.contents).toEqual(compile.mock.calls[1][1]);
    expect(submitted!.package_digest).toBe(branchPackageDigest(compile.mock.calls[1][1]));
    const hash = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
    expect(result.compile_attempts).toEqual([{ n: 1, decision: "accept", errors: 0, diagnostics: [],
      source_decoded: { entities: decoded.entities, original_sha256: hash(source), compiled_sha256: hash(decoded.source) } }]);
    expect(log).toHaveBeenCalledWith(`[harness] branch source decoded: entities=${decoded.entities} original=${hash(source).slice(0, 12)} compiled=${hash(decoded.source).slice(0, 12)} tenant=${TENANT}`);
    expect(JSON.parse(sdk.replies[0].content[0].text)).toMatchObject({ status: "VALID",
      source_decoded: decoded.entities, package_digest: submitted!.package_digest });
    expect(submitted!.manifest).not.toHaveProperty("source_decoded");
    expect(submitted!.contents).not.toHaveProperty("source_decoded");
    for (const [path, text] of Object.entries(submitted!.contents)) {
      expect(execFileSync("git", ["show", `${result.receipt.staged_commit}:${submitted!.package_path}/${path}`],
        { cwd: bare.dir })).toEqual(Buffer.from(text, "utf8"));
    }
  });

  it("reports decoded refusal diagnostics while retaining the original outcome and source", async () => {
    const source = "return 1 &lt; 2 &gt; 0;";
    const proposal = { ...fixture("good-label-writer"), main_csx: source };
    const decodedRefusal: Extract<BranchCompileOutcome, { status: "refused" }> = {
      ...parserRefusal,
      diagnostics: ["main.csx(27,8): CS1503: cannot convert from int to short"],
      structured: [{ file: "main.csx", line: 27, column: 8, code: "CS1503", message: "cannot convert from int to short" }],
    };
    const compile = vi.fn(async (_name: string, files: Record<string, string>) =>
      files["main.csx"] === source ? parserRefusal : decodedRefusal);
    setBranchCompileCheckForTests(compile);
    const { base, loop, sdk, bare } = await setup([proposal]);
    const failure = await loop.stage(TENANT, "decode refused", request(base)).catch((error: unknown) => error);
    expect((failure as AuthorLoopError).compile).toEqual({ summary: parserRefusal.summary,
      diagnostics: parserRefusal.structured, compile_attempts: [parserAttempt] });
    expect(sdk.replies[0].content[0].text).toBe(originalMessage +
      "; after Studio decoded 2 HTML escapes the source still fails: " + decodedRefusal.diagnostics[0]);
    expect(compile).toHaveBeenCalledTimes(2);
    expect(compile.mock.calls[1][1]["main.csx"]).toBe("return 1 < 2 > 0;");
    expect(proposal.main_csx).toBe(source);
    expect(git(bare.dir, ["rev-parse", "refs/leaf/changes/" + CHANGE])).toBe(base);
  });

  it.each(["original", "decoded", "short"])("bounds the refusal message with a %s diagnostic list", async mode => {
    const source = "return 1 &lt; 2;";
    const diagnostics = Array.from({ length: 20 }, (_, i) =>
      `main.csx(${i + 1},8): CS1503: ${mode === "short" ? "conversion failed" : "x".repeat(500)}`);
    const original = mode === "original" ? { ...parserRefusal, diagnostics } : parserRefusal;
    setBranchCompileCheckForTests(async (_name, files) =>
      files["main.csx"] === source ? original : { ...refusal, diagnostics });
    const { base, loop, sdk } = await setup([{ ...fixture("good-label-writer"), main_csx: source }]);
    await expect(loop.stage(TENANT, "bounded refusal", request(base))).rejects.toThrow();
    const hint = " (source contains HTML entities; write C# operators such as && < > literally)";
    const originalPart = ("compile failed: " + original.diagnostics.slice(0, 10).join("; ")).slice(0, 1500 - hint.length) + hint;
    const suffix = "; after Studio decoded 1 HTML escapes the source still fails: " + diagnostics.slice(0, 5).join("; ");
    const prefix = "V18: branch-tools/label-writer/main.csx: ";
    const message = sdk.replies[0].content[0].text.slice(prefix.length);
    expect(message).toBe(originalPart + suffix.slice(0, 1500 - originalPart.length));
    expect(message.length).toBeLessThanOrEqual(1500);
    expect(message).toContain(hint);
    if (mode === "short") expect(message).not.toContain(diagnostics[5]);
  });

  it.each(["unavailable", "throws"])("retains the original refusal if decoding recheck %s", async mode => {
    const proposal = { ...fixture("good-label-writer"), main_csx: "return 1 &lt; 2;" };
    let calls = 0;
    const compile = vi.fn(async (): Promise<BranchCompileOutcome> => {
      if (++calls === 1) return parserRefusal;
      if (mode === "throws") throw new Error("retry checker error");
      return {
        status: "unavailable", code: "timeout", ms: 1, summary: { pass: false, decision: "unavailable", errors: 0 },
      };
    });
    setBranchCompileCheckForTests(compile);
    const { base, loop, sdk } = await setup([proposal]);
    const failure = await loop.stage(TENANT, "decode refused", request(base)).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(AuthorLoopError);
    // toEqual: the one original attempt row carries no source_decoded.
    expect((failure as AuthorLoopError).compile).toEqual({ summary: parserRefusal.summary,
      diagnostics: parserRefusal.structured, compile_attempts: [parserAttempt] });
    expect(compile).toHaveBeenCalledTimes(2);
    expect(sdk.replies[0].content[0].text).toBe(originalMessage);
  });

  it.each(["semantic", "scan", "malformed", "unavailable", "lib-parser"])("never decodes a %s refusal", async mode => {
    const outcome: BranchCompileOutcome = mode === "unavailable" ? {
      status: "unavailable", code: "busy", ms: 1, summary: { pass: false, decision: "unavailable", errors: 0 },
    } : mode === "malformed" ? { ...parserRefusal, decision: "malformed",
      summary: { pass: false, decision: "malformed", errors: 1 } } : {
      ...parserRefusal, structured: [{ ...parserRefusal.structured[0],
        code: mode === "scan" ? "scan" : mode === "semantic" ? "CS0117" : "CS1525",
        file: mode === "lib-parser" ? "lib/helper.csx" : "main.csx" }],
    };
    const compile = vi.fn(async () => outcome);
    setBranchCompileCheckForTests(compile);
    const { base, loop } = await setup([{ ...fixture("good-label-writer"), main_csx: "return 1 &lt; 2;" }]);
    await expect(loop.stage(TENANT, "do not decode", request(base))).rejects.toThrow();
    expect(compile).toHaveBeenCalledTimes(1);
  });

  it("never decodes what an attempt row cannot disclose", async () => {
    const compile = vi.fn(async () => parserRefusal);
    setBranchCompileCheckForTests(compile);
    const proposal = { ...fixture("good-label-writer"), main_csx: "return 1 &lt; 2;" };
    const many = { ...proposal, main_csx: "return 1 " + "&lt;".repeat(10_001) + " 2;" };
    const { base, loop } = await setup([many, ...Array.from({ length: 8 }, () => proposal)]);
    await expect(loop.stage(TENANT, "disclosure bounds", request(base))).rejects.toThrow();
    // Too many entities: one call. Rows 2..8: two calls each. Past row 8: one call.
    expect(compile).toHaveBeenCalledTimes(1 + 7 * 2 + 1);
  });

  it("does not decode a failing source containing literal entities", async () => {
    const proposal = { ...fixture("good-label-writer"), main_csx: 'var s = "&lt;"; return 1 &lt; 2;' };
    const compile = vi.fn(async () => parserRefusal);
    setBranchCompileCheckForTests(compile);
    const { base, loop, sdk } = await setup([proposal]);
    await expect(loop.stage(TENANT, "literal entities", request(base))).rejects.toThrow();
    expect(compile).toHaveBeenCalledTimes(1);
    expect(sdk.replies[0].content[0].text).toBe(originalMessage);
  });

  it("does not decode an operator entity whose name a lib file declares", async () => {
    const proposal = { ...fixture("good-label-writer"), main_csx: '#load "lib/names.csx"\nreturn 1 &lt; 2;',
      lib: { "names.csx": "int lt = 1;\n" } };
    const compile = vi.fn(async () => parserRefusal);
    setBranchCompileCheckForTests(compile);
    const { base, loop, sdk } = await setup([proposal]);
    await expect(loop.stage(TENANT, "lib names", request(base))).rejects.toThrow();
    expect(compile).toHaveBeenCalledTimes(1);
    expect(sdk.replies[0].content[0].text).toBe(originalMessage);
  });

  it("discards a decoded candidate that fails package revalidation", async () => {
    const prepare = branchSubmission.prepareBoundBranchPackage;
    vi.spyOn(branchSubmission, "prepareBoundBranchPackage").mockImplementation((proposal, ...args) => {
      if ((proposal as BranchToolProposal).main_csx.includes("1 < 2")) {
        throw new BranchPackageError([{ rule: "V15", path: "params", message: "expected a safe integer" }]);
      }
      return prepare(proposal, ...args);
    });
    const compile = vi.fn(async () => parserRefusal);
    setBranchCompileCheckForTests(compile);
    const { base, loop, sdk } = await setup([{ ...fixture("good-label-writer"), main_csx: "return 1 &lt; 2;" }]);
    await expect(loop.stage(TENANT, "revalidate", request(base))).rejects.toMatchObject({
      compile: { summary: parserRefusal.summary, compile_attempts: [parserAttempt] },
    });
    expect(compile).toHaveBeenCalledTimes(1);
    expect(sdk.replies[0].content[0].text).toBe(originalMessage);
  });

  it("returns the diagnostics and entity hint, then stages the corrected proposal", async () => {
    const good = fixture("good-label-writer");
    const broken = { ...good, main_csx: "return true &amp;&amp; false;" };
    setBranchCompileCheckForTests(async (_name, files) => files["main.csx"] === broken.main_csx ? refusal : {
      status: "accept", ms: 3, allowlistSha256: ALLOWLIST_SHA, summary: { pass: true, decision: "accept", errors: 0 },
    });
    const { sdk, base, bare, loop, ports, coordination } = await setup([broken, good]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    vi.spyOn(harness.loop, "stage").mockImplementation(loop.stage.bind(loop));
    const server = harness.listen(0);
    servers.push(server);
    const response = await fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: TENANT, description: "write label", ...request(base) }),
    });
    expect(response.status).toBe(200);
    const { receipt, compile_attempts } = await response.json() as Awaited<ReturnType<typeof loop.stage>>;
    expect(compile_attempts).toEqual([refusedAttempt, { n: 2, decision: "accept", errors: 0, diagnostics: [] }]);
    expect(coordination.recordStaged).toHaveBeenCalledWith({ ...receipt, compile_attempts });
    expect(receipt).not.toHaveProperty("compile_attempts");
    expect(sdk.replies[0].isError).toBe(true);
    expect(sdk.replies[0].content[0].text).toBe("V18: branch-tools/label-writer/main.csx: compile failed: " +
      refusal.diagnostics.join("; ") + " (source contains HTML entities; write C# operators such as && < > literally)");
    expect(sdk.replies[1].isError).toBeUndefined();
    expect(git(bare.dir, ["show", receipt.staged_commit + ":branch-tools/label-writer/main.csx"])).toBe(good.main_csx.trim());
  });

  it.each(["spawn_failed", "not_configured"] as const)("refuses %s without a staged commit", async code => {
    if (code === "spawn_failed") setBranchCompileCheckForTests(async () => ({
      status: "unavailable", code, ms: 0, summary: { pass: false, decision: "unavailable", errors: 0 },
    }));
    const { sdk, base, bare, coordination, loop } = await setup([fixture("good-label-writer")]);
    await expect(loop.stage(TENANT, "write label", request(base))).rejects.toMatchObject({ status: 422, reason });
    expect(sdk.replies[0].isError).toBe(true);
    expect(sdk.replies[0].content[0].text).toContain(`V18: branch-tools/label-writer/main.csx: compile check unavailable (${code})`);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(base);
    expect(coordination.recordStaged).not.toHaveBeenCalled();
  });

  it("returns the last compiler summary and diagnostic locations alongside the 422 reason", async () => {
    const proposal = fixture("good-label-writer");
    const firstSource = "// first attempt";
    setBranchCompileCheckForTests(async (_name, files) => files["main.csx"] === firstSource ? {
      status: "unavailable", code: "busy", ms: 4, summary: { pass: false, decision: "unavailable", errors: 0 },
    } : refusal);
    const { base, ports, loop } = await setup([{ ...proposal, main_csx: firstSource }, proposal]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    vi.spyOn(harness.loop, "stage").mockImplementation(loop.stage.bind(loop));
    const server = harness.listen(0);
    servers.push(server);
    const response = await fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: TENANT, description: "write label", ...request(base) }),
    });
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual({ error: {
      message: "branch author session ended without a validated package: last submission refused by V18",
      diagnostics: [],
      reason: { ...reason, submit_calls: 2 },
      compile: { summary: refusal.summary, diagnostics: refusal.structured, compile_attempts: [
        { n: 1, decision: "unavailable", errors: 0, diagnostics: [] }, { ...refusedAttempt, n: 2 },
      ] },
    } });
  });
});

describe("POST /author/stage target routing", () => {
  function stageRequest(server: Server, body: Record<string, unknown>) {
    return fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
    });
  }

  it("refuses malformed approved names before starting an author session", async () => {
    const { ports, base } = await setup([]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    const stage = vi.spyOn(harness.loop, "stage");
    const server = harness.listen(0);
    servers.push(server);
    for (const toolName of [null, 42, "", "Wrong", "a--b", "a".repeat(65), "count-lines\n"]) {
      const response = await stageRequest(server, { tenant_id: TENANT, description: "count", ...request(base), toolName });
      expect(response.status).toBe(400);
    }
    expect(stage).not.toHaveBeenCalled();
  });

  it("lets the Studio author correct a name refused by the approved plan", async () => {
    const { ports, base, studioRun } = await setup([]);
    const toolName = "approved-counter";
    studioRun.mockImplementationOnce(input => {
      expect(input.systemPrompt).toContain(`The approved tool name is ${toolName}.`);
      return new FakeAgentRunner().run({ ...input, toolset: {
        ...input.toolset,
        submitTool(proposal) {
          expect(() => input.toolset.submitTool(proposal)).toThrow(`V-name: tool name must be ${toolName}`);
          return input.toolset.submitTool({ ...proposal, name: toolName });
        },
      } });
    });
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    const server = harness.listen(0);
    servers.push(server);
    const response = await stageRequest(server, {
      tenant_id: TENANT, description: "count entities", ...request(base), target: "studio", toolName,
    });
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toMatchObject({ tool: { name: toolName } });
    expect(body).not.toHaveProperty("compile_attempts");
    expect(studioRun).toHaveBeenCalledOnce();
  });

  it("lets the Branch author correct a name before the compiler sees the package", async () => {
    const toolName = "approved-counter";
    const proposal = fixture("good-panel-count");
    const { base, sdk, ports, loop } = await setup([
      proposal, { ...proposal, name: "Bad Name" }, { ...proposal, name: toolName },
    ]);
    vi.stubEnv("LEAF_BRANCH_COMPILE_CHECK", undefined);
    const compile = vi.fn(async (_name: string): Promise<BranchCompileOutcome> => ({
      status: "accept", ms: 3, allowlistSha256: ALLOWLIST_SHA, summary: { pass: true, decision: "accept", errors: 0 },
    }));
    setBranchCompileCheckForTests(compile);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    vi.spyOn(harness.loop, "stage").mockImplementation(loop.stage.bind(loop));
    const server = harness.listen(0);
    servers.push(server);
    const response = await stageRequest(server, { tenant_id: TENANT, description: "count panels", ...request(base), toolName });
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ receipt: { package_path: `branch-tools/${toolName}` } });
    expect(sdk.prompt()).toContain(`The approved tool name is ${toolName}.`);
    for (const reply of sdk.replies.slice(0, 2)) {
      expect(reply).toMatchObject({ isError: true, content: [{ text: `V-name: tool name must be ${toolName}` }] });
    }
    expect(sdk.replies[2].isError).toBeUndefined();
    expect(compile.mock.calls.map(([name]) => name)).toEqual([toolName]);
    expect(sdk.run).toHaveBeenCalledOnce();
  });

  it("validates and forwards the revision base and returns a typed loop refusal", async () => {
    const { ports, base } = await setup([]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    const reason = { code: "branch_revision_target_missing" };
    const stage = vi.spyOn(harness.loop, "stage").mockRejectedValue(new AuthorLoopError("missing", 422, [], reason));
    const server = harness.listen(0);
    servers.push(server);
    const url = "http://127.0.0.1:" + (server.address() as AddressInfo).port + "/author/stage";
    const send = (revisionBaseCommit: string) => fetch(url, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: TENANT, description: "revise", ...request(base),
        targetToolName: "label-writer", revisionBaseCommit }),
    });
    const invalid = await send("xyz");
    expect(invalid.status).toBe(400);
    expect(await invalid.json()).toEqual({ error: { message: "revisionBaseCommit must be a 40-hex commit" } });
    expect(stage).not.toHaveBeenCalled();
    const refused = await send(base);
    expect(stage).toHaveBeenCalledWith(TENANT, "revise", { ...request(base), targetToolName: "label-writer", revisionBaseCommit: base });
    expect(refused.status).toBe(422);
    expect(await refused.json()).toEqual({ error: { message: "missing", diagnostics: [], reason } });
  });

  it.each([undefined, "studio", "branch"])("passes target %s without adding a studio option", async target => {
    const { ports, base } = await setup([]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    const stage = vi.spyOn(harness.loop, "stage").mockResolvedValue({ receipt: {
      contract: "leaf.customization.v1", tenant_id: TENANT, change_set_id: CHANGE,
      state: "staged", base_commit: base, staged_commit: base, catalog_digest: "b".repeat(64),
      platform_release: "sha256:test-platform", workspace_contract_digest: "a".repeat(64), idempotency_key: `stage-${CHANGE}`,
    } });
    const server = harness.listen(0);
    servers.push(server);
    const { target: _target, ...options } = request(base);
    const response = await fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ tenant_id: TENANT, description: "author package", ...options, ...(target === undefined ? {} : { target }) }),
    });
    expect(response.status).toBe(200);
    expect(stage).toHaveBeenCalledExactlyOnceWith(TENANT, "author package", {
      ...options, ...(target === "branch" ? { target: "branch" } : {}),
    });
  });

  it.each<[unknown]>([[null], [1], [false], [{}], [[]], ["other"], ["Branch"], [""]])("rejects invalid target %j before calling the loop", async target => {
    const { ports } = await setup([]);
    const harness = createHarness(ports, { auth: { enabled: false, secret: "" } });
    const stage = vi.spyOn(harness.loop, "stage");
    const server = harness.listen(0);
    servers.push(server);
    const response = await fetch(`http://127.0.0.1:${(server.address() as AddressInfo).port}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ target }),
    });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({ error: { message: 'target must be "studio" or "branch"' } });
    expect(stage).not.toHaveBeenCalled();
  });
});

describe("accepted Branch session lifecycle", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  type Reply = Awaited<ReturnType<Mounted["handler"]>>;
  type Scenario = (submit: () => Promise<Reply>) => AsyncGenerator<unknown>;
  const assistant = (id?: string, input = 12, output = 7) => ({ type: "assistant",
    message: { ...(id ? { id } : {}), model: "fixture-model", usage: { input_tokens: input, output_tokens: output } } });
  const success = { type: "result", subtype: "success", is_error: false, num_turns: 3,
    usage: { input_tokens: 101, output_tokens: 42 }, total_cost_usd: 0.125 };
  const hang = () => new Promise<never>(() => {});

  function session(scenario: Scenario, options: { acceptedGraceMs?: number; timeoutMs?: number } = {}) {
    const root = temp();
    const mounted: Mounted[] = [];
    const close = vi.fn();
    let queryOptions: Record<string, unknown> = {};
    const input: BranchAuthorInput = {
      description: "session lifecycle", systemPrompt: "branch prompt", repoDir: root,
      grant: { kind: "oauth", oauthToken: "fixture-session-grant" }, fsTenantRepo: new FsTenantRepo(root),
      submitBranchToolProposal: vi.fn(async proposal => submitBranchToolProposal(root, proposal)),
    };
    const sdk: BranchAuthorSdk = {
      tool(name, _description, _shape, handler) { return { name, handler }; },
      createSdkMcpServer(config) { mounted.push(...config.tools as Mounted[]); return config; },
      query(config) {
        queryOptions = config.options;
        return { close, [Symbol.asyncIterator]: () => scenario(() => mounted.find(tool =>
          tool.name === "submitBranchToolProposal")!.handler(fixture("good-label-writer") as unknown as Record<string, unknown>)) };
      },
    };
    const run = () => runBranchTargetSession(input, {
      sdkImport: async () => sdk, acceptedGraceMs: 10, timeoutMs: 100, ...options,
    });
    const hook = async () => {
      const hooks = queryOptions.hooks as { PostToolUse: Array<{ matcher: string; hooks: Array<() => Promise<unknown>> }> };
      expect(hooks.PostToolUse[0].matcher).toBe("mcp__branch_author__submitBranchToolProposal");
      return hooks.PostToolUse[0].hooks[0]();
    };
    return { run, close, input, hook, options: () => queryOptions };
  }

  it("keeps acceptance after the grace when the SDK hangs and reports only observed usage", async () => {
    expect(BRANCH_ACCEPTED_GRACE_MS).toBe(5_000);
    const transport = session(async function* (submit) {
      yield assistant("message-1");
      expect((await submit()).isError).toBeUndefined();
      await hang();
    });
    let ended = false;
    const result = transport.run().then(value => { ended = true; return value; });
    await vi.advanceTimersByTimeAsync(9);
    expect(ended).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect((await result).telemetry).toEqual({ turns: 1, input_tokens: 12, output_tokens: 7,
      models: ["fixture-model"], usage_incomplete: true });
    expect(transport.close).toHaveBeenCalledOnce();
    expect((transport.options().abortController as AbortController).signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("keeps acceptance when the wall-clock limit expires first", async () => {
    const transport = session(async function* (submit) {
      await submit();
      yield assistant("message-1");
      await hang();
    }, { acceptedGraceMs: 500, timeoutMs: 20 });
    const result = transport.run();
    await vi.advanceTimersByTimeAsync(20);
    expect((await result).telemetry).toMatchObject({ usage_incomplete: true, input_tokens: 12, output_tokens: 7 });
    expect(transport.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["throw", "rate-event", "rate-error", "spend-cap", "auth-error"])("keeps acceptance after SDK %s", async mode => {
    const transport = session(async function* (submit) {
      await submit();
      yield assistant("observed");
      if (mode === "throw") throw new Error("late SDK error");
      if (mode === "rate-event") yield { type: "rate_limit_event", rate_limit_info: { status: "rejected" } };
      if (mode === "rate-error") yield { ...assistant("limited"), error: "rate_limit" };
      if (mode === "spend-cap") yield assistant("expensive", 500_001, 2);
      if (mode === "auth-error") yield { ...assistant("auth"), error: "authentication_failed" };
    });
    const result = await transport.run();
    expect(result.preview).toContain("authored");
    expect(result.telemetry).toMatchObject({ usage_incomplete: true });
    expect(result.telemetry).not.toHaveProperty("total_cost_usd");
    expect(transport.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([
    { ...success }, { ...success, is_error: true }, { ...success, subtype: "error_max_turns" },
  ])("keeps acceptance and authoritative result telemetry: %j", async final => {
    const transport = session(async function* (submit) {
      await submit();
      yield assistant("observed");
      yield final;
      await hang();
    });
    const result = await transport.run();
    expect(result.telemetry).toEqual({ turns: 3, input_tokens: 101, output_tokens: 42,
      models: ["fixture-model"], total_cost_usd: 0.125 });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("still fails without acceptance at the wall-clock limit", async () => {
    const transport = session(async function* () { yield assistant(); await hang(); });
    const result = expect(transport.run()).rejects.toThrow("wall-clock limit");
    await vi.advanceTimersByTimeAsync(100);
    await result;
    expect(transport.close).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not accept a submission handler still pending at the limit", async () => {
    let finish!: (value: ReturnType<typeof submitBranchToolProposal>) => void;
    const transport = session(async function* (submit) { await submit(); yield success; });
    const pending = new Promise<ReturnType<typeof submitBranchToolProposal>>(resolve => { finish = resolve; });
    transport.input.submitBranchToolProposal = vi.fn(() => pending);
    const result = expect(transport.run()).rejects.toThrow("wall-clock limit");
    await vi.advanceTimersByTimeAsync(100);
    await result;
    finish({ manifest: prepareBranchPackage(fixture("good-label-writer"), "late").manifest,
      files: [], contents: {}, package_path: "branch-tools/label-writer", package_digest: "a".repeat(64) });
    await vi.advanceTimersByTimeAsync(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("asks the SDK to stop only after acceptance", async () => {
    const replies: unknown[] = [];
    const transport = session(async function* (submit) {
      replies.push(await transport.hook());
      await submit();
      replies.push(await transport.hook());
      yield success;
    });
    await transport.run();
    expect(replies).toEqual([{}, { continue: false, stopReason: "branch package accepted" }]);
  });

  it("deduplicates fallback usage by message id using its highest token values", async () => {
    const transport = session(async function* (submit) {
      yield assistant("same", 10, 2);
      yield assistant("same", 12, 1);
      yield assistant("same", 11, 7);
      yield assistant(undefined, 3, 4);
      yield assistant(undefined, 5, 6);
      await submit();
      await hang();
    });
    const result = transport.run();
    await vi.advanceTimersByTimeAsync(10);
    expect((await result).telemetry).toEqual({ turns: 3, input_tokens: 20, output_tokens: 17,
      models: ["fixture-model"], usage_incomplete: true });
  });

  it("observes late drain rejection after grace without an unhandled rejection", async () => {
    let rejectDrain!: (error: Error) => void;
    const pending = new Promise<never>((_resolve, reject) => { rejectDrain = reject; });
    const unhandled = vi.fn();
    process.on("unhandledRejection", unhandled);
    try {
      const transport = session(async function* (submit) { await submit(); await pending; });
      const result = transport.run();
      await vi.advanceTimersByTimeAsync(10);
      expect((await result).preview).toContain("authored");
      rejectDrain(new Error("late drain rejection"));
      await vi.advanceTimersByTimeAsync(0);
      expect(unhandled).not.toHaveBeenCalled();
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      process.off("unhandledRejection", unhandled);
    }
  });
});
