// O2/O3 vertical slice, harness side (contract/OPERATOR.md Lane D): a dispatched
// operator job runs ONLY in the isolated disposable worker. It fails closed on a
// non-isolating substrate (so broad work never runs un-jailed), forces network
// deny + disposable workspace, and the worker scrubs the env.

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LocalProcessSubstrate,
  OperatorWorkerManager,
} from "../src/operatorWorker/workerManager.js";
import {
  buildOperatorWorkerEnvelope,
  dispatchOperatorWorkerJob,
  OperatorWorkerDispatchError,
} from "../src/operatorWorker/operatorWorkerDispatch.js";

const REQ = {
  commands: ["echo hi"],
  idempotencyKey: "k1",
  principalSubject: "auth0|op-test",
  sessionId: "opsess-1",
};

let root: string;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "op-disp-"));
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

describe("operator worker dispatch (Lane D binding)", () => {
  it("forces isolation into the envelope: disposable workspace, egress denied, capped timeout", () => {
    const env = buildOperatorWorkerEnvelope({ ...REQ, timeoutMs: 9_999_999_999 });
    expect(env.workspace).toBe("disposable");
    expect(env.network).toEqual([]); // no egress
    expect(env.timeoutMs).toBeLessThanOrEqual(1_800_000);
    expect(env.principalSubject).toBe("auth0|op-test");
  });

  it("rejects an unbounded dispatch request", () => {
    expect(() => buildOperatorWorkerEnvelope({ ...REQ, commands: [] }))
      .toThrow(OperatorWorkerDispatchError);
    expect(() => buildOperatorWorkerEnvelope({ ...REQ, commands: Array(51).fill("x") }))
      .toThrow(OperatorWorkerDispatchError);
  });

  it("FAILS CLOSED: broad work is refused on a non-isolating substrate", async () => {
    // The shipped LocalProcessSubstrate is non-isolating. Without the explicit
    // test opt-in, the manager refuses to run broad work rather than run it
    // un-jailed, so operator work never executes outside a real isolation
    // boundary.
    const manager = new OperatorWorkerManager(
      new LocalProcessSubstrate(root), path.join(root, "_a"));
    await expect(dispatchOperatorWorkerJob(manager, REQ))
      .rejects.toThrow("substrate_not_isolating");
  });

  it("runs inside the isolated worker with a scrubbed env (no secret crosses)", async () => {
    // With the test opt-in (a real substrate in production), the job runs in the
    // disposable worker; plant a canary secret and prove it is absent from the
    // job's own view of the environment.
    process.env.AWS_SECRET_ACCESS_KEY = "canary-aws-secret";
    const manager = new OperatorWorkerManager(
      new LocalProcessSubstrate(root), path.join(root, "_a"),
      { allowNonIsolatedSubstrate: true });
    try {
      const receipt = await dispatchOperatorWorkerJob(manager, {
        ...REQ,
        commands: [process.platform === "win32" ? "set" : "env"],
      });
      expect(JSON.stringify(receipt)).not.toContain("canary-aws-secret");
    } finally {
      delete process.env.AWS_SECRET_ACCESS_KEY;
    }
  });
});
