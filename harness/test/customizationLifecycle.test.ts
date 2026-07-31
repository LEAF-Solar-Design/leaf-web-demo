import { execFileSync } from "node:child_process";
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
