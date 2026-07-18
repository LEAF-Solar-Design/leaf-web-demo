/**
 * Design-time-ONLY invariant test.
 *
 * POST /run-registered returns a CONTRACT section 3 result envelope, and a spy on
 * AgentRunner.run() proves the run path NEVER constructs the Agent SDK / never
 * enters the author loop. A positive control confirms the spy actually fires on
 * the author path (so "never called" is meaningful, not a dead wire).
 */

import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";

import { createHarness } from "../src/server.js";
import type { AgentRunInput, AgentRunResult, HarnessPorts } from "../src/ports/index.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");

describe("design-time-only invariant", () => {
  let server: Server;
  let baseUrl: string;
  let runner: FakeAgentRunner;
  let runSpy: MockInstance<(input: AgentRunInput) => Promise<AgentRunResult>>;
  let broker: FakeBrokerApsClient;

  beforeEach(() => {
    runner = new FakeAgentRunner();
    runSpy = vi.spyOn(runner, "run");
    broker = new FakeBrokerApsClient();
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker,
      agentRunner: runner,
    };
    server = createHarness(ports).listen(0);
    const addr = server.address() as AddressInfo;
    baseUrl = `http://127.0.0.1:${addr.port}`;
  });

  afterEach(() => {
    runSpy.mockRestore();
    server.close();
  });

  it("POST /run-registered returns a section-3 envelope WITHOUT calling AgentRunner.run", async () => {
    const res = await fetch(`${baseUrl}/run-registered`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ tool: "count-by-layer", params: {}, dwg: "rooftop_demo" }),
    });

    expect(res.status).toBe(200);
    const env = (await res.json()) as Record<string, unknown>;

    // CONTRACT section 3 envelope shape
    for (const key of ["ok", "tool", "version", "result", "overlay", "timing_ms", "cost", "error"]) {
      expect(env).toHaveProperty(key);
    }
    expect(env.ok).toBe(true);
    expect(env.tool).toBe("count-by-layer");
    expect((env.result as { counts?: Record<string, number> }).counts).toBeTruthy();

    // THE invariant: the SDK boundary was never touched on the run path.
    expect(runSpy).not.toHaveBeenCalled();
    expect(runner.calls).toBe(0);

    // ...and it DID go through the broker (the only APS egress).
    expect(broker.calls).toHaveLength(1);
    expect(broker.calls[0]!.apsLive).toBe(false);
  });

  it("positive control: the author path DOES call AgentRunner.run (spy is live)", async () => {
    const res = await fetch(`${baseUrl}/author`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ description: "count entities per layer" }),
    });
    expect(res.status).toBe(200);
    expect(runSpy).toHaveBeenCalledTimes(1);
    expect(runner.calls).toBe(1);
  });
});
