/** HTTP lifecycle boundary: caller auth, non-effective staging, and exact publish. */

import { execFileSync } from "node:child_process";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { CustomizationCoordination, HarnessPorts, StagedCustomizationReceipt } from "../src/ports/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "test-harness-secret";
const TENANT = "route-customization-tenant";
const CHANGE = "11111111-1111-4111-8111-111111111111";
const DIGEST = "a".repeat(64);

function git(dir: string, args: string[]): string {
  return execFileSync("git", args, { cwd: dir, encoding: "utf8" }).trim();
}

class Coordination implements CustomizationCoordination {
  staged: StagedCustomizationReceipt | null = null;
  approved = false;
  authorizeCalls = 0;

  async recordStaged(receipt: StagedCustomizationReceipt): Promise<void> {
    this.staged = receipt;
  }

  async authorizePublish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<void> {
    this.authorizeCalls += 1;
    if (!this.approved || !this.staged || expectedMainSha !== receipt.base_commit ||
      JSON.stringify(this.staged) !== JSON.stringify(receipt)) {
      throw new Error("publish requires the approved exact staged receipt");
    }
  }
}

describe("customization routes", () => {
  let server: Server;
  let baseUrl: string;
  let bareDir: string;
  let coordination: Coordination;

  beforeEach(async () => {
    const tenantRepo = new FakeTenantRepoProvider(FIXTURE);
    coordination = new Coordination();
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo,
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
      customizationCoordination: coordination,
    };
    bareDir = (await tenantRepo.bare(TENANT)).dir;
    server = createHarness(ports, { auth: { enabled: true, secret: SECRET } }).listen(0);
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(() => server.close());

  function stageBody() {
    return {
      tenant_id: TENANT,
      description: "count entities per layer",
      changeSetId: CHANGE,
      expectedBaseSha: git(bareDir, ["rev-parse", "refs/heads/main"]),
      platformRelease: "sha256:platform-release",
      workspaceContractDigest: DIGEST,
      idempotencyKey: `stage-${CHANGE}`,
    };
  }

  it("requires harness caller auth before staging", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const response = await fetch(`${baseUrl}/author/stage`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(stageBody()),
    });
    expect(response.status).toBe(401);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
  });

  it("stops staging before any repository or coordination mutation when authored execution is off", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const previous = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "0";
    try {
      const response = await fetch(`${baseUrl}/author/stage`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": SECRET },
        body: JSON.stringify(stageBody()),
      });
      expect(response.status).toBe(403);
      expect(coordination.staged).toBeNull();
      expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
      expect(git(bareDir, ["for-each-ref", "--format=%(refname)", "refs/leaf/changes"]))
        .toBe("");
    } finally {
      if (previous === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
      else process.env.LEAF_AUTHORED_EXECUTION = previous;
    }
  });

  it("stages through AuthorLoop without moving main", async () => {
    const main = git(bareDir, ["rev-parse", "refs/heads/main"]);
    const response = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    expect(response.status).toBe(200);
    const body = await response.json() as { receipt: StagedCustomizationReceipt };
    expect(body.receipt.base_commit).toBe(main);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
    expect(git(bareDir, ["rev-parse", `refs/leaf/changes/${CHANGE}`])).toBe(body.receipt.staged_commit);
  });

  it("publishes only the exact approved receipt", async () => {
    const stagedResponse = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    const staged = await stagedResponse.json() as { receipt: StagedCustomizationReceipt };
    const main = staged.receipt.base_commit;
    coordination.approved = true;

    const altered = { ...staged.receipt, catalog_digest: "b".repeat(64) };
    const rejected = await fetch(`${baseUrl}/author/publish`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ receipt: altered, expectedMainSha: main }),
    });
    expect(rejected.status).toBe(500);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);

    const published = await fetch(`${baseUrl}/author/publish`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({ receipt: staged.receipt, expectedMainSha: main }),
    });
    expect(published.status).toBe(200);
    expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(staged.receipt.staged_commit);
  });

  it("stops publish before authorization or main mutation when authored execution is off", async () => {
    const stagedResponse = await fetch(`${baseUrl}/author/stage`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify(stageBody()),
    });
    const staged = await stagedResponse.json() as { receipt: StagedCustomizationReceipt };
    const main = staged.receipt.base_commit;
    coordination.approved = true;
    const previous = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "0";
    try {
      const response = await fetch(`${baseUrl}/author/publish`, {
        method: "POST",
        headers: { "content-type": "application/json", "x-harness-secret": SECRET },
        body: JSON.stringify({ receipt: staged.receipt, expectedMainSha: main }),
      });
      expect(response.status).toBe(403);
      expect(coordination.authorizeCalls).toBe(0);
      expect(git(bareDir, ["rev-parse", "refs/heads/main"])).toBe(main);
      expect(git(bareDir, ["rev-parse", `refs/leaf/changes/${CHANGE}`]))
        .toBe(staged.receipt.staged_commit);
    } finally {
      if (previous === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
      else process.env.LEAF_AUTHORED_EXECUTION = previous;
    }
  });
});
