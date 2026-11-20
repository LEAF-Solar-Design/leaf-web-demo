import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { InMemoryTransport } from "@modelcontextprotocol/sdk/inMemory.js";
import { createHarness } from "../src/server.js";
import { AuthorLoop } from "../src/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { HarnessPorts, StagedCustomizationReceipt } from "../src/ports/index.js";
import {
  branchTargetPrompt, runBranchTargetSession,
  type BranchAuthorInput, type BranchAuthorSdk,
} from "../src/vendor/mushy-author/agent/branchTargetPrompt.js";
import { submitBranchToolProposal } from "../src/vendor/mushy-author/agent/tools/submitBranchToolProposal.js";
import { FsTenantRepo } from "../src/vendor/mushy-author/agent/tools/fsTenantRepo.js";
import * as repoPathSafety from "../src/vendor/mushy-author/agent/tools/repoPathSafety.js";
import {
  BranchPackageError, prepareBranchPackage, validateBranchPackage,
  type BranchToolProposal,
} from "../src/vendor/mushy-author/registry/branchPackageSchema.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(HERE, "fixtures", "branch-target");
const TENANT = "branch-author-tenant";
const CHANGE = "11111111-1111-4111-8111-111111111111";
const SECOND = "22222222-2222-4222-8222-222222222222";
const NOW = new Date("2026-11-20T00:00:00.000Z");
const ALLOWLIST_SHA = "03830bbe9eb579e291a0e588556b2597497892de92a0485d15d269013b8c11dd";
const dirs: string[] = [];
const servers: Server[] = [];

afterEach(async () => {
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
    expect(prompt).toContain("server S1 validator is authoritative");
  });

  it.each(["good-panel-count", "good-label-writer"])("writes exactly the recorded %s package", name => {
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
  it.each(["good-panel-count", "good-label-writer"])("stages %s through the branch SDK tool without registering Python", async name => {
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
    const permission = sdk.options().canUseTool as (name: string, args: Record<string, unknown>) => Promise<{ behavior: string }>;
    expect(await permission("Bash", {})).toMatchObject({ behavior: "deny" });
    expect(await permission("mcp__branch_author__submitBranchToolProposal", {})).toMatchObject({ behavior: "allow" });
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
      input.submitBranchToolProposal(fixture("good-label-writer"));
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
      input.submitBranchToolProposal(fixture("good-panel-count"));
      return { preview: "test", telemetry: { input_tokens: 17, output_tokens: 3 } };
    });
    await loop.stage(TENANT, "panels", request(base));
    expect(acquireGrant).toHaveBeenCalledWith(TENANT);
    expect(settleGrant).toHaveBeenCalledWith(TENANT, "branch-lease", { usage: { cost_tokens: 20 }, stop_reason: "end_turn" });
  });
});

describe("POST /author/stage target routing", () => {
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
