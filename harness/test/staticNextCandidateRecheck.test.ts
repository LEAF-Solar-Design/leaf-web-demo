import { expect, it, vi } from "vitest";
import { nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import { createStaticNextCandidateRecheck } from "../src/projectBuild/staticNextCandidateRecheck.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(1), projectId: id(2), repoKey: id(3) };
const witness = { jobId: id(4), sourceRequestId: id(5), version: 2, receiptDigest: "a".repeat(64),
  sourceCommit: "b".repeat(40), sourceTree: "c".repeat(40) };
const scope = { ...authority, jobId: witness.jobId, sourceCommit: witness.sourceCommit, sourceTree: witness.sourceTree };
const lookup = { tenant_id: authority.tenantId, organization_id: authority.organizationId, project_id: authority.projectId,
  repo_key: authority.repoKey, job_id: witness.jobId, source_commit: witness.sourceCommit, source_tree: witness.sourceTree };
const publication = { source_request_id: witness.sourceRequestId, version: witness.version, receipt_digest: witness.receiptDigest };
const admitted = { ...lookup, ...publication, schema: "leaf.browser-static-candidate-recheck.v1",
  execution_id: nextRequestDigest(scope) };

function recheck(send: typeof fetch) {
  return createStaticNextCandidateRecheck({ appUrl: "https://app.example.test/base", dispatchSecret: "fixture-dispatch", fetch: send });
}

it("sends the exact dispatch operation and uses the persisted complete publication for reduced checks", async () => {
  const send = vi.fn<typeof fetch>(async () => Response.json(admitted));
  const client = recheck(send);
  await expect(client.loadCandidate(authority, witness.sourceRequestId, witness, "write")).resolves.toMatchObject({
    authority, sourceRequestId: witness.sourceRequestId, version: witness.version, receiptDigest: witness.receiptDigest,
  });
  await expect(client.isCurrent(scope, "read")).resolves.toBe(true);
  expect(send.mock.calls.map(([url, opts]) => ({
    url: String(url), method: opts?.method, redirect: opts?.redirect, headers: opts?.headers,
    body: JSON.parse(String(opts?.body)),
  }))).toEqual(["write", "read"].map(operation => ({
    url: "https://app.example.test/internal/project-static-preview/candidate-recheck",
    method: "POST", redirect: "error",
    headers: { "Content-Type": "application/json", "X-Tenant-Id": authority.tenantId, "X-Dispatch-Secret": "fixture-dispatch" },
    body: { ...lookup, ...(operation === "write" && publication), operation },
  })));
  await expect(client.loadCandidate(authority, id(9), witness, "read")).resolves.toBeNull();
  expect(send).toHaveBeenCalledTimes(2);
});

it.each([
  ["tenant", { tenant_id: id(9) }], ["job", { job_id: id(9) }], ["receipt", { receipt_digest: "f".repeat(64) }],
  ["version", { version: 3 }], ["execution", { execution_id: "0".repeat(64) }], ["actor", { actor_binding_id: id(8) }],
])("refuses a mismatched %s reply", async (_name, change) => {
  const client = recheck(async () => Response.json({ ...admitted, ...change }));
  await expect(client.loadCandidate(authority, witness.sourceRequestId, witness, "read")).resolves.toBeNull();
});

it.each([
  ["revoked", () => new Response(null, { status: 403 })],
  ["missing", () => new Response(null, { status: 404 })],
  ["oversized", () => new Response(" ".repeat(16385))],
  ["unavailable", () => { throw new Error("offline"); }],
])("fails closed when App is %s", async (_name, reply) => {
  await expect(recheck(async () => reply()).isCurrent(scope, "write")).resolves.toBe(false);
});
