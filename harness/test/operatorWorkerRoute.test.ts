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
  tenantId: "tenant-route-test",
  roleRevision: 7,
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

async function post(baseUrl: string, body: unknown, headers: Record<string, string> = {}, route = "/operator/worker/dispatch") {
  return fetch(`${baseUrl}${route}`, {
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
    const active = await allowed.json() as { worker_id: string; run_id: string };
    let settled = false;
    for (let attempt = 0; attempt < 50; attempt += 1) {
      const resolved = await post(baseUrl, {
        workerId: active.worker_id,
        runId: active.run_id,
        principalSubject: BODY.principalSubject,
        tenantId: BODY.tenantId,
        roleRevision: BODY.roleRevision,
      }, { "X-Harness-Secret": "route-secret" }, "/operator/worker/resolve");
      const binding = await resolved.json() as { active?: boolean };
      if (binding.active === false) {
        settled = true;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    expect(settled).toBe(true);
  });

  it("accepts an active worker identity before execution finishes", async () => {
    const baseUrl = boot({ operatorWorker: { manager: isolatedManager() } });
    const res = await post(baseUrl, BODY);
    expect(res.status).toBe(200);
    const receipt = (await res.json()) as {
      status: string; worker_id: string; run_id: string;
    };
    expect(receipt.status).toBe("running");
    expect(receipt.worker_id).toMatch(/^opworker-/);
    expect(receipt.run_id).toMatch(/^oprun-/);
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

  it("authenticates resolve and cancels only the exact owned active worker", async () => {
    const baseUrl = boot({
      auth: { enabled: true, secret: "route-secret" },
      operatorWorker: { manager: isolatedManager() },
    });
    const long = process.platform === "win32" ? "ping -n 30 127.0.0.1 > NUL" : "sleep 30";
    const accepted = await post(baseUrl, { ...BODY, commands: [long], idempotencyKey: "route-cancel" },
      { "X-Harness-Secret": "route-secret" });
    const target = await accepted.json() as { worker_id: string; run_id: string };
    const controlTarget = { workerId: target.worker_id, runId: target.run_id };
    const denied = await post(baseUrl, { ...controlTarget, principalSubject: "auth0|attacker", tenantId: BODY.tenantId, roleRevision: BODY.roleRevision }, {}, "/operator/worker/resolve");
    expect(denied.status).toBe(401);
    const cancelled = await post(baseUrl, { ...controlTarget, principalSubject: BODY.principalSubject, tenantId: BODY.tenantId, roleRevision: BODY.roleRevision },
      { "X-Harness-Secret": "route-secret" }, "/operator/worker/cancel");
    expect(cancelled.status).toBe(200);
    expect((await cancelled.json()).status).toBe("cancelled");
  });
});
