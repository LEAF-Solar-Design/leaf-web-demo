// O2/O3 route wiring e2e (contract/OPERATOR.md Lane D): the harness serves
// POST /operator/worker/dispatch behind the F5 caller-auth gate, forwarding to
// the isolated OperatorWorkerManager. Unconfigured -> 501 (ships dark);
// non-isolating substrate -> 503 fail-closed refusal; bounds violation -> 400.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import {
  LocalProcessSubstrate,
  OperatorWorkerManager,
} from "../src/operatorWorker/workerManager.js";

const BODY = {
  commands: ["echo route-e2e"],
  idempotencyKey: "route-k1",
  principalSubject: "auth0|op-route-test",
  sessionId: "opsess-route-1",
};

function makePorts(): HarnessPorts {
  return {
    oauth: new FakeOAuthGrantProvider(),
    tenantRepo: new FakeTenantRepoProvider(process.cwd()),
    broker: new FakeBrokerApsClient(),
    agentRunner: new FakeAgentRunner(),
  };
}

let root: string;
let server: Server | null = null;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "op-route-"));
});

afterEach(() => {
  server?.close();
  server = null;
  fs.rmSync(root, { recursive: true, force: true });
});

function boot(opts?: Parameters<typeof createHarness>[1]): string {
  server = createHarness(makePorts(), opts).listen(0);
  const address = server.address() as AddressInfo;
  return `http://127.0.0.1:${address.port}`;
}

function isolatedManager(): OperatorWorkerManager {
  // LocalProcessSubstrate is non-isolating; the explicit opt-in stands in for a
  // real isolating substrate exactly as the manager's own tests do.
  return new OperatorWorkerManager(
    new LocalProcessSubstrate(root), path.join(root, "_a"),
    { allowNonIsolatedSubstrate: true });
}

async function post(baseUrl: string, body: unknown, headers: Record<string, string> = {}) {
  return fetch(`${baseUrl}/operator/worker/dispatch`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify(body),
  });
}

describe("operator worker route (Lane D wiring e2e)", () => {
  it("ships dark: 501 when no operator worker manager is wired", async () => {
    const baseUrl = boot();
    const res = await post(baseUrl, BODY);
    expect(res.status).toBe(501);
  });

  it("sits behind the F5 caller-auth gate: 401 without the shared secret, 200 with it", async () => {
    const baseUrl = boot({
      auth: { enabled: true, secret: "route-secret" },
      operatorWorker: { manager: isolatedManager() },
    });
    const denied = await post(baseUrl, BODY);
    expect(denied.status).toBe(401);
    const allowed = await post(baseUrl, BODY, { "X-Harness-Secret": "route-secret" });
    expect(allowed.status).toBe(200);
  });

  it("round trip: a dispatched job runs in the worker and returns its receipt", async () => {
    const baseUrl = boot({ operatorWorker: { manager: isolatedManager() } });
    const res = await post(baseUrl, BODY);
    expect(res.status).toBe(200);
    const receipt = (await res.json()) as {
      status: string; stdoutTail: string; workspaceRemoved: boolean;
    };
    expect(receipt.status).toBe("succeeded");
    expect(receipt.stdoutTail).toContain("route-e2e");
    expect(receipt.workspaceRemoved).toBe(true);
  });

  it("FAILS CLOSED over the wire: a non-isolating substrate is a 503 refusal, not execution", async () => {
    const manager = new OperatorWorkerManager(
      new LocalProcessSubstrate(root), path.join(root, "_a"));
    const baseUrl = boot({ operatorWorker: { manager } });
    const res = await post(baseUrl, BODY);
    expect(res.status).toBe(503);
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe("substrate_not_isolating");
  });

  it("rejects a bounds violation with 400 before any execution: missing principal", async () => {
    const baseUrl = boot({ operatorWorker: { manager: isolatedManager() } });
    const res = await post(baseUrl, { ...BODY, principalSubject: "" });
    expect(res.status).toBe(400);
  });
});
