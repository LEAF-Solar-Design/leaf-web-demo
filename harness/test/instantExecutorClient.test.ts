import { createServer } from "node:http";
import { describe, expect, it } from "vitest";
import { HttpInstantExecutorClient } from "../src/ports/impl/instantExecutorClient.js";
import type { InstantInvocation, InstantSessionAssignment } from "../src/ports/index.js";

const digest = `sha256:${"a".repeat(64)}`;
const assignment = (endpoint: string): InstantSessionAssignment => ({
  contract: "leaf.instant-execution/v1", assignment_id: "11111111-1111-4111-8111-111111111111", tenant_id: "tenant-demo", session_id: "22222222-2222-4222-8222-222222222222", executor_id: "executor-local-001", executor_endpoint: endpoint, binding_epoch: 1, lease_id: "77777777-7777-4777-8777-777777777777", lease_token: "x".repeat(32), execution_class: "instant", effective_catalog_digest: digest, code_digest: digest, artifact_digest: digest, drawing_context: invocation.drawing_context, issued_at: "2026-01-01T00:00:00Z", expires_at: "2099-01-01T00:00:00Z",
});
const invocation: InstantInvocation = { contract: "leaf.instant-execution/v1", invocation_id: "44444444-4444-4444-8444-444444444444", tenant_id: "tenant-demo", session_id: "22222222-2222-4222-8222-222222222222", assignment_id: "11111111-1111-4111-8111-111111111111", binding_epoch: 1, lease_id: "77777777-7777-4777-8777-777777777777", effective_catalog_digest: digest, code_digest: digest, artifact_digest: digest, deadline_at: "2099-01-01T00:00:00Z", capability: { capability_id: "drawing.read", tool_id: "instant-read", tool_version: "1.0.0" }, params: {}, drawing_context: { drawing_id: "rooftop-demo", version_id: "55555555-5555-4555-8555-555555555555", content_digest: digest, geometry_ref: "drawing-context:rooftop-ref-001" } };

describe("HttpInstantExecutorClient", () => {
  it("reuses a keep-alive direct executor connection and sends only invocation metadata", async () => {
    let connections = 0;
    let requests = 0;
    const server = createServer((req, res) => {
      requests += 1;
      expect(req.url).toBe("/v1/invoke");
      expect(req.headers.authorization).toBe(`Bearer ${"x".repeat(32)}`);
      res.setHeader("content-type", "application/json");
      res.statusCode = requests === 1 ? 200 : 403;
      res.end(JSON.stringify({ contract: "leaf.instant-execution/v1", invocation_id: invocation.invocation_id, tenant_id: invocation.tenant_id, session_id: invocation.session_id, status: requests === 1 ? "succeeded" : "failed", code_digest: digest, completed_at: new Date().toISOString(), ...(requests === 1 ? { result: {} } : { error: { code: "SESSION_EXPIRED" } }) }));
    });
    server.on("connection", () => { connections += 1; });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    const client = new HttpInstantExecutorClient();
    const a = assignment(`http://127.0.0.1:${(address as import("node:net").AddressInfo).port}`);
    await client.invoke(a, invocation);
    const failed = await client.invoke(a, { ...invocation, invocation_id: "44444444-4444-4444-8444-444444444445" });
    client.close();
    await new Promise<void>((resolve) => server.close(() => resolve()));
    expect(connections).toBe(1);
    expect(failed.status).toBe("failed");
    expect(failed.error?.code).toBe("SESSION_EXPIRED");
  });
});
