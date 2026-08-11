import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { AuthorLoop } from "../src/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type {
  CustomizationCoordination,
  AgentRunner,
  HarnessPorts,
  StagedCustomizationReceipt,
  TenantMutationFence,
} from "../src/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const TENANT = "customization-tenant";
const CHANGE_A = "11111111-1111-4111-8111-111111111111";
const CHANGE_B = "22222222-2222-4222-8222-222222222222";
const DIGEST = "a".repeat(64);

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

class TestCustomizationCoordination implements CustomizationCoordination {
  readonly staged = new Map<string, StagedCustomizationReceipt>();
  readonly approved = new Set<string>();

  async recordStaged(receipt: StagedCustomizationReceipt): Promise<void> {
    this.staged.set(receipt.change_set_id, receipt);
  }

  async authorizePublish(receipt: StagedCustomizationReceipt, _expectedMainSha: string): Promise<void> {
    const stored = this.staged.get(receipt.change_set_id);
    if (!stored || JSON.stringify(stored) !== JSON.stringify(receipt) || !this.approved.has(receipt.change_set_id)) {
      throw new Error("publish requires an approved, exact staged receipt");
    }
  }
}

class FencedFakeTenantRepoProvider extends FakeTenantRepoProvider {
  leaseLost = false;

  override async withTenantLease<T>(
    tenantId: string,
    action: (runFenced: TenantMutationFence) => Promise<T>,
  ): Promise<T> {
    this.leaseTenants.push(tenantId);
    return action(async (operation) => {
      if (this.leaseLost) throw new Error("tenant writer lease lost");
      return operation();
    });
  }
}

function request(changeSetId: string, expectedBaseSha: string) {
  return {
    changeSetId,
    expectedBaseSha,
    platformRelease: "sha256:platform-release",
    workspaceContractDigest: DIGEST,
    idempotencyKey: `stage-${changeSetId}`,
  };
}

async function setup() {
  const tenantRepo = new FakeTenantRepoProvider(FIXTURE);
  const coordination = new TestCustomizationCoordination();
  const agent = new FakeAgentRunner();
  const ports: HarnessPorts = {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo,
    broker: new FakeBrokerApsClient(),
    agentRunner: agent,
    customizationCoordination: coordination,
  };
  const bare = await tenantRepo.bare(TENANT);
  return { agent, bare, coordination, loop: new AuthorLoop(ports), tenantRepo };
}

async function setupFenced() {
  const tenantRepo = new FencedFakeTenantRepoProvider(FIXTURE);
  const coordination = new TestCustomizationCoordination();
  const agent = new FakeAgentRunner();
  const ports: HarnessPorts = {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo,
    broker: new FakeBrokerApsClient(),
    agentRunner: agent,
    customizationCoordination: coordination,
  };
  const bare = await tenantRepo.bare(TENANT);
  return { agent, bare, coordination, loop: new AuthorLoop(ports), tenantRepo };
}

describe("customizationLifecycle", () => {
  it("stages one exact registry-row removal without moving main or reordering retained rows", async () => {
    const { bare, loop } = await setup();
    const seed = await new FakeTenantRepoProvider(FIXTURE).checkout(TENANT);
    const registry = JSON.parse(git(seed.dir, ["show", "HEAD:registry.json"])) as {
      tools: Array<Record<string, unknown>>;
    };
    const retained: Record<string, unknown> = {
      name: "retained-tool", note: "escaped \\\"value\\\"", ...registry.tools[0],
    };
    retained.name = "retained-tool";
    const retainedRaw = `{"note":"escaped \\\"value\\\"",  "name":"retained-tool", "capabilities" : ${JSON.stringify(retained.capabilities)}, "description":${JSON.stringify(retained.description)}, "engine_op":${JSON.stringify(retained.engine_op)}, "params":${JSON.stringify(retained.params)}, "returns":${JSON.stringify(retained.returns)}}`;
    const irregular = `{"note":"decoy \\\"tools\\\": [1]", "nested":{"tools":[]}, "tools":[\n  ${JSON.stringify(registry.tools[0])},\n\t${retainedRaw}\n]}\n`;
    writeFileSync(join(seed.dir, "registry.json"), irregular);
    git(seed.dir, ["add", "registry.json"]);
    git(seed.dir, ["-c", "user.name=Leaf Test", "-c", "user.email=test@leafdesign.ai", "commit", "-m", "seed retained row"]);
    git(seed.dir, ["push", "--force", bare.dir, "HEAD:main"]);
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir });
    const digest = createHash("sha256").update(raw).digest("hex");

    const staged = await loop.stageRemoval(TENANT, {
      ...request(CHANGE_A, base), toolName: "count-by-layer",
      expectedCatalogDigest: digest,
    });
    const result = JSON.parse(git(bare.dir, ["show", `${staged.receipt.staged_commit}:registry.json`])) as {
      tools: Array<Record<string, unknown>>;
    };
    expect(result.tools).toEqual([JSON.parse(retainedRaw)]);
    const stagedRaw = execFileSync(
      "git", ["show", `${staged.receipt.staged_commit}:registry.json`],
      { cwd: bare.dir, encoding: "utf8" },
    );
    expect(stagedRaw).toContain(retainedRaw);
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });

  it("refuses stale digest and absent removal targets without advancing main", async () => {
    const { bare, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    await expect(loop.stageRemoval(TENANT, {
      ...request(CHANGE_A, base), toolName: "count-by-layer",
      expectedCatalogDigest: "0".repeat(64),
    })).rejects.toThrow("effective catalog digest changed");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);

    const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir });
    const digest = createHash("sha256").update(raw).digest("hex");
    await expect(loop.stageRemoval(TENANT, {
      ...request(CHANGE_B, base), toolName: "missing-tool",
      expectedCatalogDigest: digest,
    })).rejects.toThrow("removal target cardinality is not one");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });

  it("refuses a duplicate removal target", async () => {
    const { bare, loop } = await setup();
    const seed = await new FakeTenantRepoProvider(FIXTURE).checkout(TENANT);
    const registry = JSON.parse(git(seed.dir, ["show", "HEAD:registry.json"])) as {
      tools: Array<Record<string, unknown>>;
    };
    registry.tools.push({ ...registry.tools[0] });
    writeFileSync(join(seed.dir, "registry.json"), `${JSON.stringify(registry, null, 2)}\n`);
    git(seed.dir, ["add", "registry.json"]);
    git(seed.dir, ["-c", "user.name=Leaf Test", "-c", "user.email=test@leafdesign.ai", "commit", "-m", "seed duplicate row"]);
    git(seed.dir, ["push", "--force", bare.dir, "HEAD:main"]);
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir });
    const digest = createHash("sha256").update(raw).digest("hex");

    await expect(loop.stageRemoval(TENANT, {
      ...request(CHANGE_A, base), toolName: "count-by-layer",
      expectedCatalogDigest: digest,
    })).rejects.toThrow("removal target cardinality is not one");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });

  it("rejects duplicate root tools keys as a malformed registry", async () => {
    const { bare, loop } = await setup();
    const seed = await new FakeTenantRepoProvider(FIXTURE).checkout(TENANT);
    const row = JSON.parse(git(seed.dir, ["show", "HEAD:registry.json"])).tools[0];
    const malformed = `{"tools":[${JSON.stringify(row)}],"tools":[${JSON.stringify(row)}]}\n`;
    writeFileSync(join(seed.dir, "registry.json"), malformed);
    git(seed.dir, ["add", "registry.json"]);
    git(seed.dir, ["-c", "user.name=Leaf Test", "-c", "user.email=test@leafdesign.ai", "commit", "-m", "seed duplicate root key"]);
    git(seed.dir, ["push", "--force", bare.dir, "HEAD:main"]);
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir });
    const digest = createHash("sha256").update(raw).digest("hex");
    await expect(loop.stageRemoval(TENANT, {
      ...request(CHANGE_A, base), toolName: "count-by-layer",
      expectedCatalogDigest: digest,
    })).rejects.toThrow("tenant registry is malformed");
  });
  it("revises exactly the explicitly bound authored tool without adding a catalog entry", async () => {
    const tenantRepo = new FakeTenantRepoProvider(FIXTURE);
    const coordination = new TestCustomizationCoordination();
    const agent: AgentRunner = {
      async run(input) {
        const submitted = input.toolset.submitTool({
          name: "count-by-layer",
          description: "Counts every model-space entity per layer with corrected logic.",
          engine_op: "count_by_layer",
          params: { type: "object", properties: {}, required: [] },
          returns: { type: "object" },
          capabilities: ["drawing.read"],
          source: "def run(intake, params):\n    return ({'counts': {}, 'revised': True}, None)\n",
          session: "revision-test",
        });
        return {
          tool: submitted.tool, code: submitted.code, preview: "revised",
          files: submitted.files, sourceReceipt: submitted.receipt,
        };
      },
    };
    const loop = new AuthorLoop({
      oauth: new FakeOAuthGrantProvider(), tenantRepo,
      broker: new FakeBrokerApsClient(), agentRunner: agent,
      customizationCoordination: coordination,
    });
    const bare = await tenantRepo.bare(TENANT);
    const seed = await tenantRepo.checkout(TENANT);
    const existing = JSON.parse(git(seed.dir, ["show", "HEAD:registry.json"]))
      .tools[0] as Record<string, unknown>;
    mkdirSync(join(seed.dir, "tools", "count-by-layer"), { recursive: true });
    writeFileSync(
      join(seed.dir, "tools", "count-by-layer", "tool.py"),
      "def run(intake, params):\n    return ({'counts': {}}, None)\n",
    );
    writeFileSync(
      join(seed.dir, "tools", "count-by-layer", "tool.json"),
      JSON.stringify({ ...existing, entry: "tool.py" }, null, 2) + "\n",
    );
    git(seed.dir, ["add", "."]);
    git(seed.dir, [
      "-c", "user.name=Leaf Test", "-c", "user.email=test@leafdesign.ai",
      "commit", "-m", "seed authored package",
    ]);
    git(seed.dir, ["push", "--force", bare.dir, "HEAD:main"]);
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);

    const staged = await loop.stage(TENANT, "fix count-by-layer", {
      ...request(CHANGE_A, base), targetToolName: "count-by-layer",
    });
    const registry = JSON.parse(git(bare.dir, [
      "show", `${staged.receipt.staged_commit}:registry.json`,
    ])) as { tools: Array<{ name: string; description: string }> };

    expect(registry.tools).toHaveLength(1);
    expect(registry.tools[0]?.name).toBe("count-by-layer");
    expect(registry.tools[0]?.description).toContain("corrected logic");
    expect((registry.tools[0] as { version?: string })?.version).toBe("1.0.1");
    expect(git(bare.dir, [
      "show", `${staged.receipt.staged_commit}:tools/count-by-layer/tool.py`,
    ])).toContain("'revised': True");
  });

  it("rejects a generated name that differs from the durable revision target", async () => {
    const { bare, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    await expect(loop.stage(TENANT, "count entities per layer", {
      ...request(CHANGE_A, base), targetToolName: "count-by-layer",
    })).rejects.toThrow("keep the bound target name");
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_A}`])).toBe(base);
  });

  it("does not advance the reserved change ref after the tenant writer lease is lost", async () => {
    const { agent, bare, loop, tenantRepo } = await setupFenced();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const originalRun = agent.run.bind(agent);
    agent.run = async (input) => {
      const result = await originalRun(input);
      tenantRepo.leaseLost = true;
      return result;
    };

    await expect(
      loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base)),
    ).rejects.toThrow("tenant writer lease lost");

    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", "--verify", `refs/leaf/changes/${CHANGE_A}`])).toBe(base);
  });

  it("does not move main when the tenant writer lease is lost during publish authorization", async () => {
    const { bare, coordination, loop, tenantRepo } = await setupFenced();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const staged = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));
    coordination.approved.add(CHANGE_A);
    const originalAuthorize = coordination.authorizePublish.bind(coordination);
    coordination.authorizePublish = async (receipt, expectedMainSha) => {
      await originalAuthorize(receipt, expectedMainSha);
      tenantRepo.leaseLost = true;
    };

    await expect(loop.publish(staged.receipt, base)).rejects.toThrow("tenant writer lease lost");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_A}`]))
      .toBe(staged.receipt.staged_commit);
  });

  it("holds the tenant writer lease across stage and publish", async () => {
    const { bare, coordination, loop, tenantRepo } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const staged = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));
    coordination.approved.add(CHANGE_A);
    await loop.publish(staged.receipt, base);

    expect(tenantRepo.leaseTenants).toEqual([TENANT, TENANT]);
  });

  it("stages in a private ref without moving main, and the model receives no publish capability", async () => {
    const { agent, bare, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    let modelToolset: object | undefined;
    const originalRun = agent.run.bind(agent);
    agent.run = async (input) => {
      modelToolset = input.toolset;
      return originalRun(input);
    };

    const staged = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));

    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_A}`])).toBe(staged.receipt.staged_commit);
    expect(staged.receipt).toMatchObject({
      contract: "leaf.customization.v1",
      tenant_id: TENANT,
      state: "staged",
      base_commit: base,
      idempotency_key: `stage-${CHANGE_A}`,
    });
    expect(Object.keys(staged.receipt).sort()).toEqual([
      "base_commit",
      "catalog_digest",
      "change_set_id",
      "contract",
      "idempotency_key",
      "platform_release",
      "staged_commit",
      "state",
      "tenant_id",
      "workspace_contract_digest",
    ]);
    expect(Object.isFrozen(staged.receipt)).toBe(true);
    expect(modelToolset).not.toHaveProperty("publish");
  });

  it("stages safely when the isolated author worktree has a different owner", async () => {
    const { agent, bare, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const originalRun = agent.run.bind(agent);
    const previousDifferentOwner = process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
    agent.run = async (input) => {
      const result = await originalRun(input);
      process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = "1";
      return result;
    };

    let staged: Awaited<ReturnType<AuthorLoop["stage"]>>;
    try {
      staged = await loop.stage(TENANT, "count entities per layer", request(CHANGE_B, base));
    } finally {
      if (previousDifferentOwner === undefined) {
        delete process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER;
      } else {
        process.env.GIT_TEST_ASSUME_DIFFERENT_OWNER = previousDifferentOwner;
      }
    }

    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_B}`]))
      .toBe(staged.receipt.staged_commit);
  });

  it("keeps two staged changes isolated", async () => {
    const { bare, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const first = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));
    const second = await loop.stage(TENANT, "list layer names", request(CHANGE_B, base));

    expect(first.receipt.staged_commit).not.toBe(second.receipt.staged_commit);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_A}`])).toBe(first.receipt.staged_commit);
    expect(git(bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_B}`])).toBe(second.receipt.staged_commit);
    expect(git(bare.dir, ["show", "--format=", "--name-only", first.receipt.staged_commit])).toContain("tools/count-entities-per-layer/tool.py");
    expect(git(bare.dir, ["show", "--format=", "--name-only", second.receipt.staged_commit])).toContain("tools/list-layer-names/tool.py");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });

  it("resumes a staged private ref after the coordination callback fails", async () => {
    const { agent, bare, coordination, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const originalRecord = coordination.recordStaged.bind(coordination);
    let callbacks = 0;
    coordination.recordStaged = async (receipt) => {
      callbacks += 1;
      if (callbacks === 1) throw new Error("coordination unavailable");
      await originalRecord(receipt);
    };
    let authorRuns = 0;
    const originalRun = agent.run.bind(agent);
    agent.run = async (input) => {
      authorRuns += 1;
      return originalRun(input);
    };

    await expect(
      loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base)),
    ).rejects.toThrow("coordination unavailable");
    const stagedSha = git(bare.dir, [
      "rev-parse",
      `refs/leaf/changes/${CHANGE_A}`,
    ]);

    const recovered = await loop.stage(
      TENANT,
      "count entities per layer",
      request(CHANGE_A, base),
    );

    expect(recovered.receipt.staged_commit).toBe(stagedSha);
    expect(authorRuns).toBe(1);
    expect(callbacks).toBe(2);
  });

  it("rejects a stale main base during publish", async () => {
    const { bare, coordination, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const first = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));
    const second = await loop.stage(TENANT, "list layer names", request(CHANGE_B, base));
    coordination.approved.add(CHANGE_A);
    coordination.approved.add(CHANGE_B);

    await loop.publish(first.receipt, base);
    await expect(loop.publish(second.receipt, base)).rejects.toThrow("Git ref conflict");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(first.receipt.staged_commit);
  });

  it("rejects publish when the staged private ref no longer matches its receipt", async () => {
    const { bare, coordination, loop } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const staged = await loop.stage(TENANT, "count entities per layer", request(CHANGE_A, base));
    coordination.approved.add(CHANGE_A);
    git(bare.dir, ["update-ref", `refs/leaf/changes/${CHANGE_A}`, base, staged.receipt.staged_commit]);

    await expect(loop.publish(staged.receipt, base)).rejects.toThrow("Git ref conflict");
    expect(git(bare.dir, ["rev-parse", "refs/heads/main"])).toBe(base);
  });
});
