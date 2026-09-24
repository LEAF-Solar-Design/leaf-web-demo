/** /author/publish maps each publish failure class to the status the app's state machine keys on. */

import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthorLoopError } from "../src/agent/authorLoop.js";
import { createHarness, publishErrorResponse } from "../src/server.js";
import type { Harness } from "../src/server.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import { GitRefConflictError } from "../src/ports/impl/tenantChangeRepo.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { ForgePublicationError } from "../src/vendor/mushy-author/ports/impl/forgeRemoteAuthority.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = join(HERE, "fixtures", "tenant-repo");
const SECRET = "test-harness-secret";
const MAIN = "a".repeat(40);
const STAGED = "b".repeat(40);

const CASES: Array<[string, Error, number, string]> = [
  ["a lost expected-head CAS", new GitRefConflictError("refs/heads/main", MAIN, STAGED), 409, "publish_conflict"],
  ["a remote that did not accept", new ForgePublicationError("not-published", "remote main moved"), 409,
    "publish_not_accepted"],
  ["a refused credential", new ForgePublicationError("refused", "credential revoked"), 403, "publish_refused"],
  ["an unknown remote outcome", new ForgePublicationError("unknown", "readback failed"), 503,
    "publish_outcome_unknown"],
];

describe("author publish error mapping", () => {
  let server: Server;
  let baseUrl: string;
  let harness: Harness;
  let previousAuthored: string | undefined;

  beforeEach(() => {
    // The route refuses before publishing unless authored execution is armed; the
    // package test script arms it, a bare `vitest run` does not.
    previousAuthored = process.env.LEAF_AUTHORED_EXECUTION;
    process.env.LEAF_AUTHORED_EXECUTION = "1";
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(FIXTURE),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    };
    harness = createHarness(ports, { auth: { enabled: true, secret: SECRET } });
    server = harness.listen(0);
    baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(() => {
    vi.restoreAllMocks();
    server.close();
    if (previousAuthored === undefined) delete process.env.LEAF_AUTHORED_EXECUTION;
    else process.env.LEAF_AUTHORED_EXECUTION = previousAuthored;
  });

  async function publish(): Promise<Response> {
    return fetch(`${baseUrl}/author/publish`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-harness-secret": SECRET },
      body: JSON.stringify({
        tenant_id: "mapping-tenant",
        receipt: { change_set_id: "11111111-1111-4111-8111-111111111111", staged_commit: STAGED },
        expectedMainSha: MAIN,
      }),
    });
  }

  it.each(CASES)("maps %s to its status and a fixed code", async (_label, error, status, code) => {
    vi.spyOn(harness.loop, "publish").mockRejectedValue(error);
    const response = await publish();
    expect(response.status).toBe(status);
    const body = await response.json();
    expect(body).toEqual({ error: code });
    // Refs and SHAs from the conflict message never reach the app.
    expect(JSON.stringify(body)).not.toContain(MAIN);
    expect(JSON.stringify(body)).not.toContain(STAGED);
  });

  it("leaves an AuthorLoopError on its own status", async () => {
    vi.spyOn(harness.loop, "publish").mockRejectedValue(
      new AuthorLoopError("remote publication did not prove the staged commit", 503));
    const response = await publish();
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({
      error: { message: "remote publication did not prove the staged commit", diagnostics: [] },
    });
  });

  it("leaves an unclassified error on the 500 catch-all", async () => {
    vi.spyOn(harness.loop, "publish").mockRejectedValue(
      new Error("publish requires the approved exact staged receipt"));
    const response = await publish();
    expect(response.status).toBe(500);
    expect(await response.json()).toEqual({ error: { message: "internal error: Error" } });
  });

  it("returns 200 with the commit when publish succeeds", async () => {
    vi.spyOn(harness.loop, "publish").mockResolvedValue({ commit: STAGED });
    const response = await publish();
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ commit: STAGED });
  });

  it("classifies only the publish error classes", () => {
    for (const [, error, status, code] of CASES) {
      expect(publishErrorResponse(error)).toEqual({ status, body: { error: code } });
    }
    expect(publishErrorResponse(new Error("other"))).toBeNull();
    expect(publishErrorResponse(new AuthorLoopError("bad", 409))).toBeNull();
    expect(publishErrorResponse("not an error")).toBeNull();
  });
});
