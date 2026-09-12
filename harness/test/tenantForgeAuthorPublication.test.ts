import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { AuthorLoop } from "../src/vendor/mushy-author/agent/authorLoop.js";
import { FakeAgentRunner } from "../src/vendor/mushy-author/ports/fakes/fakeAgentRunner.js";
import { FakeOAuthGrantProvider } from "../src/vendor/mushy-author/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/vendor/mushy-author/ports/fakes/fakeTenantRepo.js";
import { ForgeRemoteAuthority } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";
import type {
  BrokerApsClient,
  CustomizationCoordination,
  HarnessPorts,
  StagedCustomizationReceipt,
} from "../src/vendor/mushy-author/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const TENANT = "customization-tenant";
const CHANGE_A = "11111111-1111-4111-8111-111111111111";
const CHANGE_B = "22222222-2222-4222-8222-222222222222";
const DIGEST = "a".repeat(64);
const unusedBroker: BrokerApsClient = {
  async runTool() {
    throw new Error("broker is not used by removal staging");
  },
};

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
    broker: unusedBroker,
    agentRunner: agent,
    customizationCoordination: coordination,
  };
  const bare = await tenantRepo.bare(TENANT);
  return { agent, bare, coordination, loop: new AuthorLoop(ports), tenantRepo, ports };
}

async function remoteRemoval() {
  const state = await setup();
  const remote = join(dirname(state.bare.dir), "forge.git");
  git(dirname(remote), ["clone", "--bare", state.bare.dir, remote]);
  git(state.bare.dir, ["config", "remote.origin.url", remote]);
  const base = git(state.bare.dir, ["rev-parse", "refs/heads/main"]);
  const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: state.bare.dir });
  const staged = await state.loop.stageRemoval(TENANT, {
    ...request(CHANGE_A, base), toolName: "count-by-layer",
    expectedCatalogDigest: createHash("sha256").update(raw).digest("hex"),
  });
  let revoked = false;
  const authority = new ForgeRemoteAuthority({
    locate: async () => remote,
    credentials: async (tenantId, remoteUrl) => revoked ? null : { tenantId, remoteUrl },
    allowLocalPathsForTests: true,
  });
  Object.assign(state.bare, authority.bind(TENANT, state.bare.dir, remote));
  state.coordination.approved.add(CHANGE_A);
  return { ...state, remote, base, receipt: staged.receipt, revoke: () => { revoked = true; } };
}

describe("customizationLifecycle", () => {
  it("requires remote acceptance before local publication and reauthorizes completed retries", async () => {
    const f = await remoteRemoval();
    await expect(f.loop.publish(f.receipt, f.base)).resolves.toEqual({ commit: f.receipt.staged_commit });
    expect(git(f.remote, ["rev-parse", "refs/heads/main"])).toBe(f.receipt.staged_commit);
    expect(git(f.bare.dir, ["rev-parse", "refs/heads/main"])).toBe(f.receipt.staged_commit);
    f.coordination.approved.delete(CHANGE_A);
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("approved, exact staged receipt");
    f.coordination.approved.add(CHANGE_A);
    f.revoke();
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("refused");
  });

  it("does not trust a locally advanced main when remote authorization is revoked", async () => {
    const f = await remoteRemoval();
    git(f.bare.dir, ["update-ref", "refs/heads/main", f.receipt.staged_commit]);
    f.revoke();
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("refused");
    expect(git(f.remote, ["rev-parse", "refs/heads/main"])).toBe(f.base);
  });

  it("keeps local main unchanged on a refused remote publish", async () => {
    const f = await remoteRemoval();
    f.revoke();
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("refused");
    expect(git(f.bare.dir, ["rev-parse", "refs/heads/main"])).toBe(f.base);
    expect(git(f.bare.dir, ["rev-parse", `refs/leaf/changes/${CHANGE_A}`])).toBe(f.receipt.staged_commit);
  });

  it("recovers remote acceptance after loss of the local mutation fence under a new lease", async () => {
    const f = await remoteRemoval();
    let loseFence = true;
    const originalPublish = f.bare.publishAuthoritatively!;
    let accepted = false;
    f.bare.publishAuthoritatively = async (publication) => {
      const result = await originalPublish(publication);
      accepted = true;
      return result;
    };
    f.ports.tenantRepo.withTenantLease = async (_tenantId, action) => action(async (operation) => {
      if (accepted && loseFence) throw new Error("tenant repo lease was lost");
      return operation();
    });
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("lease was lost");
    expect(git(f.remote, ["rev-parse", "refs/heads/main"])).toBe(f.receipt.staged_commit);
    expect(git(f.bare.dir, ["rev-parse", "refs/heads/main"])).toBe(f.base);
    loseFence = false;
    f.coordination.approved.delete(CHANGE_A);
    await expect(f.loop.publish(f.receipt, f.base)).rejects.toThrow("approved, exact staged receipt");
    f.coordination.approved.add(CHANGE_A);
    await expect(f.loop.publish(f.receipt, f.base)).resolves.toEqual({ commit: f.receipt.staged_commit });
    expect(git(f.bare.dir, ["rev-parse", "refs/heads/main"])).toBe(f.receipt.staged_commit);
  });

  it("preserves local-only publish and its idempotent retry when no authority is configured", async () => {
    const { bare, loop, coordination } = await setup();
    const base = git(bare.dir, ["rev-parse", "refs/heads/main"]);
    const raw = execFileSync("git", ["show", `${base}:registry.json`], { cwd: bare.dir });
    const { receipt } = await loop.stageRemoval(TENANT, {
      ...request(CHANGE_A, base), toolName: "count-by-layer",
      expectedCatalogDigest: createHash("sha256").update(raw).digest("hex"),
    });
    coordination.approved.add(CHANGE_A);
    await expect(loop.publish(receipt, base)).resolves.toEqual({ commit: receipt.staged_commit });
    await expect(loop.publish(receipt, base)).resolves.toEqual({ commit: receipt.staged_commit });
  });

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
});
