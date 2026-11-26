import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, it } from "vitest";
import { createStaticNextBundleResolver } from "../src/projectBuild/codebuildStaticNextExecutor.js";
import { DiskStaticNextCustodyStore } from "../src/projectBuild/diskStaticNextCustodyStore.js";
import { nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import { createManagedStaticNextViews } from "../src/projectBuild/managedStaticNextComposition.js";
import { DiskPrivateStaticArtifacts } from "../src/projectBuild/privateStaticArtifactStore.js";
import { markStaticNextCleaned, publishStaticNextCustody } from "../src/projectBuild/staticNextCustody.js";
import type { PublishedBrowserCandidate } from "../src/projectBuild/browserCandidateBuild.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceRequestId: id(6), version: 1, receiptDigest: "a".repeat(64),
  sourceCommit: "b".repeat(40), sourceTree: "c".repeat(40) };
const executionId = nextRequestDigest({ ...authority, ...request });
const bundleSha256 = "d".repeat(64);
const buildId = "static-next:build-1";
const now = () => new Date("2026-11-25T12:00:00.000Z");

it.each([
  ["/series.json", "application/json", "[1,2,3]"],
  ["/captions.vtt", "text/vtt", "WEBVTT"],
  ["/schedule.csv", "text/csv", "box,input\n1,2"],
])("reopens %s from durable custody and rechecks its current candidate", async (path, contentType, text) => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "leaf-managed-static-view-")));
  try {
    const custodyStore = new DiskStaticNextCustodyStore(root);
    const artifactOptions = { root: join(root, "artifacts"), bindingId: id(7), isCurrent: async () => true };
    const artifacts = new DiskPrivateStaticArtifacts(artifactOptions);
    const result = { protocol: "leaf.static-next.v1" as const, executionId, bindingId: id(8),
      toolchainSha256: "e".repeat(64), bundleSha256, exitCode: 0,
      files: [{ path: "/index.html", contentType: "text/html", body: Buffer.from("<main>Preview</main>") },
        { path, contentType, body: Buffer.from(text) }] };
    const stop = { protocol: result.protocol, bindingId: result.bindingId, executionId, confirmed: true };
    let candidate: PublishedBrowserCandidate = { ...request, authority, state: "published",
      schema: "leaf.browser-project-source.v1" };
    let mapping: Buffer | null = null;
    const views = createManagedStaticNextViews({
      artifacts: new DiskPrivateStaticArtifacts(artifactOptions),
      custodyStore: new DiskStaticNextCustodyStore(root),
      resolveStaticNextBundle: createStaticNextBundleResolver({
        get: async key => key === `map/${executionId}.json` ? mapping : null,
        putIfAbsent: async () => { throw new Error("readback attempted a write"); },
      }),
      loadCandidate: async (_authority, requestId) =>
        requestId === candidate.sourceRequestId ? candidate : null,
      now,
    });
    const receipt = await publishStaticNextCustody({ authority, request, store: custodyStore,
      artifacts, result, providerBuildId: buildId, stop, now, retentionMs: 60_000 });
    await expect(views.observe(authority, request)).resolves.toMatchObject({ status: "unknown" });
    mapping = Buffer.from(JSON.stringify({ executionId, bundleSha256, buildId }));
    const asset = { artifactId: receipt.artifactId, manifestSha256: receipt.manifestSha256, path };
    await expect(views.observe(authority, request)).resolves.toMatchObject({
      status: "ready", result: { preview: { artifactId: asset.artifactId, manifestSha256: asset.manifestSha256 } },
    });
    await expect(views.asset(authority, request, asset)).resolves.toMatchObject({
      content_type: contentType, body_base64: Buffer.from(text).toString("base64"),
    });
    await expect(views.observe({ ...authority, projectId: id(9) }, request)).rejects.toThrow("preview_unavailable");
    candidate = { ...candidate, version: request.version + 1 };
    await expect(views.asset(authority, request, asset)).rejects.toThrow("preview_unavailable");
    candidate = { ...candidate, version: request.version };
    await markStaticNextCleaned({ store: custodyStore, bundleSha256, executionId, filesRemoved: false, now });
    await expect(views.observe(authority, request)).resolves.toMatchObject({ status: "unknown" });
    await expect(views.asset(authority, request, asset)).rejects.toThrow("preview_unavailable");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
