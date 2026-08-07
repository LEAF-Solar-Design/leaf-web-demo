import type { Server } from "node:http";
import type { AddressInfo } from "node:net";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createHarness } from "../src/server.js";
import type { HarnessPorts } from "../src/ports/index.js";
import { FakeAgentRunner } from "../src/ports/fakes/fakeAgentRunner.js";
import { FakeBrokerApsClient } from "../src/ports/fakes/fakeBrokerApsClient.js";
import { FakeOAuthGrantProvider } from "../src/ports/fakes/fakeOAuthGrant.js";
import { FakeTenantRepoProvider } from "../src/ports/fakes/fakeTenantRepo.js";
import type { LeafStandardServicesHumanApprovalHost } from "../src/ports/impl/leafStandardServicesResolver.js";

const SECRET = "dispatch-secret-value-for-approval-host";
const IDENTITY = {
  tenant_id: "tenant-a",
  subject_id: "subject-a",
  session_id: "session-a",
  authority_turn_id: "turn-a",
  subscription_mount_id: "mount-a",
  runner_profile_id: "spine" as const,
};

describe("standard services human approval host routes", () => {
  let server: Server;
  let baseUrl: string;
  const review = vi.fn();
  const execute = vi.fn();

  beforeEach(() => {
    review.mockReset().mockResolvedValue({ status: "pending", identity: IDENTITY });
    execute.mockReset().mockResolvedValue({ status: "completed", receipt_id: "b".repeat(64) });
    const ports: HarnessPorts = {
      oauth: new FakeOAuthGrantProvider(),
      tenantRepo: new FakeTenantRepoProvider(process.cwd()),
      broker: new FakeBrokerApsClient(),
      agentRunner: new FakeAgentRunner(),
    };
    const host = { review, execute } as unknown as LeafStandardServicesHumanApprovalHost;
    server = createHarness(ports, {
      standardServicesApproval: { dispatchSecret: SECRET, host },
    }).listen(0);
    const address = server.address() as AddressInfo;
    baseUrl = `http://127.0.0.1:${address.port}`;
  });

  afterEach(() => server.close());

  async function post(path: string, body: unknown, secret = SECRET) {
    return fetch(`${baseUrl}${path}`, {
      method: "POST",
      headers: { "content-type": "application/json", "x-dispatch-secret": secret },
      body: JSON.stringify(body),
    });
  }

  it("rejects missing or wrong dispatch authority before either host action", async () => {
    const missing = await fetch(`${baseUrl}/internal/standard-services/approvals/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({}),
    });
    const wrong = await post("/internal/standard-services/approvals/execute", {}, "wrong-secret");

    expect(missing.status).toBe(401);
    expect(wrong.status).toBe(401);
    expect(review).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
    expect(await wrong.text()).not.toContain(SECRET);
  });

  it("reviews the exact approval and exposes only its pending identity", async () => {
    const request = {
      approval_id: "approval_12345678",
      argument_digest: "a".repeat(64),
      tenant_id: "tenant-a",
      subject_id: "subject-a",
    };
    const response = await post("/internal/standard-services/approvals/review", request);

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(review).toHaveBeenCalledWith(request);
    expect(await response.json()).toEqual({ status: "pending", identity: IDENTITY });
  });

  it("sanitizes durable-store failures instead of exposing provider details", async () => {
    const privateDetail = "postgresql://user:secret@private-db/leaf";
    review.mockRejectedValueOnce(new Error(privateDetail));
    const response = await post("/internal/standard-services/approvals/review", {
      approval_id: "approval_12345678",
      argument_digest: "a".repeat(64),
      tenant_id: "tenant-a",
      subject_id: "subject-a",
    });

    expect(response.status).toBe(409);
    expect(await response.text()).not.toContain(privateDetail);
    expect(execute).not.toHaveBeenCalled();
  });

  it("returns only the safe receipt from the authenticated execution action", async () => {
    const secretToken = "secret-human-token-value";
    const response = await post("/internal/standard-services/approvals/execute", {
      approval_id: "approval_12345678",
      argument_digest: "a".repeat(64),
      identity: IDENTITY,
      human_bearer: secretToken,
      attachment: {
        bearer_token: "attachment-token",
        channel_secret: "channel-secret",
        expires_at: "2099-01-01T00:00:00.000Z",
      },
    });

    expect(response.status).toBe(200);
    expect(execute).toHaveBeenCalledTimes(1);
    const text = await response.text();
    expect(JSON.parse(text)).toEqual({ status: "completed", receipt_id: "b".repeat(64) });
    expect(text).not.toContain(secretToken);
    expect(text).not.toContain("attachment-token");
  });

  it("fails closed on an unknown path and an oversized chunked body", async () => {
    const unknown = await post("/internal/standard-services/approvals/unknown", {});
    const oversized = await post("/internal/standard-services/approvals/review", {
      padding: "x".repeat(65 * 1024),
    });

    expect(unknown.status).toBe(404);
    expect(oversized.status).toBe(413);
    expect(review).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });
});
