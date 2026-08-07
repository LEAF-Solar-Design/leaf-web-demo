// Lane D gate: disposable worker isolation, env scrub, timeout tree-kill,
// idempotency, artifact receipts, cleanup, and orphan reaping.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LocalProcessSubstrate,
  OperatorWorkerManager,
  type WorkerJobEnvelope,
} from "../src/operatorWorker/workerManager.js";

let root: string;
let manager: OperatorWorkerManager;

function envelope(partial: Partial<WorkerJobEnvelope>): WorkerJobEnvelope {
  return {
    workspace: "disposable",
    commands: ["echo hello"],
    idempotencyKey: `k-${Math.random().toString(36).slice(2)}`,
    principalSubject: "auth0|op-test",
    sessionId: "opsess-test",
    ...partial,
  };
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "op-worker-test-"));
  manager = new OperatorWorkerManager(
    new LocalProcessSubstrate(root), path.join(root, "_artifacts"));
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

describe("job env scrub", () => {
  it("no credential, secret, deploy, or cloud key crosses into a job", () => {
    process.env.LEAF_TEST_CANARY_SECRET = "canary-value";
    process.env.AWS_ACCESS_KEY_ID = "canary-aws";
    try {
      const env = manager.buildJobEnv(envelope({}));
      const keys = Object.keys(env).join(",").toUpperCase();
      for (const banned of ["AWS", "SECRET", "TOKEN", "DISPATCH", "BROKER",
                            "OPS_SECRET", "ANTHROPIC", "APS_", "DEPLOY"]) {
        expect(keys).not.toContain(banned);
      }
      expect(JSON.stringify(env)).not.toContain("canary-value");
      expect(JSON.stringify(env)).not.toContain("canary-aws");
    } finally {
      delete process.env.LEAF_TEST_CANARY_SECRET;
      delete process.env.AWS_ACCESS_KEY_ID;
    }
  });

  it("always-denied network hosts cannot be allowlisted back in", () => {
    const env = manager.buildJobEnv(envelope({
      network: ["api.leafdesign.ai", "169.254.169.254", "github.com"],
    }));
    expect(env.LEAF_OPERATOR_NET_ALLOW).toBe("github.com");
    expect(env.LEAF_OPERATOR_NET_DENY).toContain("api.leafdesign.ai");
    expect(env.LEAF_OPERATOR_NET_DENY).toContain("169.254.169.254");
  });
});

describe("execution and receipts", () => {
  it("a job runs inside its workspace and the workspace is removed", async () => {
    const receipt = await manager.submit(envelope({
      commands: ["echo one > out.txt", "echo two"],
    }));
    expect(receipt.status).toBe("succeeded");
    expect(receipt.exitCodes).toEqual([0, 0]);
    expect(receipt.workspaceRemoved).toBe(true);
  });

  it("artifacts under ./artifacts are preserved with sha256 receipts", async () => {
    const receipt = await manager.submit(envelope({
      commands: ["mkdir artifacts && echo payload > artifacts/result.txt"],
    }));
    expect(receipt.status).toBe("succeeded");
    expect(receipt.artifacts).toHaveLength(1);
    const artifact = receipt.artifacts[0];
    expect(fs.existsSync(artifact.path)).toBe(true);
    expect(artifact.sha256).toMatch(/^[a-f0-9]{64}$/);
    expect(receipt.workspaceRemoved).toBe(true);
  });

  it("a failing command stops the chain and reports failed", async () => {
    const receipt = await manager.submit(envelope({
      commands: ["exit 3", "echo never"],
    }));
    expect(receipt.status).toBe("failed");
    expect(receipt.exitCodes).toEqual([3]);
  });

  // retry absorbs taskkill latency under parallel test-file execution; the
  // assertions themselves stay strict.
  it("timeout terminates the process tree and reports timeout", { retry: 2, timeout: 30_000 }, async () => {
    const sleep = process.platform === "win32"
      ? "ping -n 30 127.0.0.1 > NUL" : "sleep 30";
    const started = Date.now();
    const receipt = await manager.submit(envelope({
      commands: [sleep],
      timeoutMs: 1500,
    }));
    expect(receipt.status).toBe("timeout");
    expect(receipt.terminatedBy).toBe("timeout");
    expect(Date.now() - started).toBeLessThan(15_000);
    expect(receipt.workspaceRemoved).toBe(true);
  });
});

describe("idempotency and validation", () => {
  it("duplicate submission under one key yields one logical job", async () => {
    const key = "stable-key-1";
    const first = await manager.submit(envelope({
      idempotencyKey: key, commands: ["echo once"],
    }));
    const second = await manager.submit(envelope({
      idempotencyKey: key, commands: ["echo twice"],
    }));
    expect(second.jobId).toBe(first.jobId);
  });

  it("invalid envelopes are refused", async () => {
    await expect(manager.submit(envelope({ commands: [] })))
      .rejects.toThrow("commands_invalid");
    await expect(manager.submit(
      envelope({ workspace: "host" as never })))
      .rejects.toThrow("workspace_invalid");
    await expect(manager.submit(envelope({ idempotencyKey: "" })))
      .rejects.toThrow("idempotency_key_required");
  });
});

describe("orphan reaping", () => {
  it("a crashed manager's workspace is removed on the next boot", () => {
    const orphan = path.join(root, "op-worker-dead-job");
    fs.mkdirSync(orphan, { recursive: true });
    const reaped = OperatorWorkerManager.reapOrphans(root, new Set());
    expect(reaped).toEqual(["op-worker-dead-job"]);
    expect(fs.existsSync(orphan)).toBe(false);
  });
});
