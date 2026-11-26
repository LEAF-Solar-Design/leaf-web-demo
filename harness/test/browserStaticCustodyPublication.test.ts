import { createHash } from "node:crypto";
import { mkdtemp, realpath, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it, vi } from "vitest";
import { BrowserCandidateBuildAdapter } from "../src/projectBuild/browserCandidateBuild.js";
import type { PublishedBrowserCandidate } from "../src/projectBuild/browserCandidateBuild.js";
import { DiskStaticNextCustodyStore } from "../src/projectBuild/diskStaticNextCustodyStore.js";
import { GenericNextProducer, nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import { ExecutorManagedNextRuntime } from "../src/projectBuild/managedNextRuntime.js";
import type { StaticNextJobExecutor } from "../src/projectBuild/managedNextRuntime.js";
import { createPrivateBuildServices } from "../src/projectBuild/privateBuildLifecycle.js";
import { DiskPrivateStaticArtifacts } from "../src/projectBuild/privateStaticArtifactStore.js";
import { loadPrivateStaticView } from "../src/projectBuild/staticNextCustody.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceRequestId: id(6), version: 1, receiptDigest: "a".repeat(64),
  sourceCommit: "b".repeat(40), sourceTree: "c".repeat(40) };
const bundle = Buffer.from("immutable revision input");
const bundleSha256 = createHash("sha256").update(bundle).digest("hex");
const executionId = nextRequestDigest({ ...authority, ...request });
const now = () => new Date("2026-11-25T12:00:00.000Z");
const roots: string[] = [];
afterEach(async () => { await Promise.all(roots.splice(0).map(root => rm(root, { recursive: true, force: true }))); });

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>(done => { resolve = done; });
  return { promise, resolve };
}

async function fixture(options: {
  path?: string; contentType?: string; text?: string; failure?: "cleanup" | "authority" | "provider";
} = {}) {
  const root = await realpath(await mkdtemp(join(tmpdir(), "leaf-cleaned-static-build-")));
  roots.push(root);
  const store = new DiskStaticNextCustodyStore(root);
  const files = [{ path: "/index.html", contentType: "text/html", body: Buffer.from("<main>Private</main>") },
    { path: options.path ?? "/data.json", contentType: options.contentType ?? "application/json",
      body: Buffer.from(options.text ?? "{}") }];
  const startedStop = deferred();
  const finishStop = deferred();
  let row: PublishedBrowserCandidate = { ...request, authority,
    schema: "leaf.browser-project-source.v1", state: "published" };
  const executor: StaticNextJobExecutor = {
    bindingId: id(7), toolchainSha256: "d".repeat(64), isolation: "filesystem-and-network",
    async run(input) {
      return { ...input, exitCode: 0, files,
        providerBuildId: options.failure === "provider" ? undefined : "static-next:observed-build" };
    },
    async stop(identity) {
      startedStop.resolve();
      await finishStop.promise;
      if (options.failure === "authority") row = { ...row, version: 2 };
      return { ...identity, confirmed: options.failure !== "cleanup" };
    },
  };
  const artifacts = new DiskPrivateStaticArtifacts({
    root: join(root, "artifacts"), bindingId: id(8), isCurrent: async () => true,
  });
  const producer = new GenericNextProducer({
    repository: { async exportProjectSourceBundle() {
      return { sourceCommit: request.sourceCommit, sourceTree: request.sourceTree,
        bundle, bundleSha256, sizeBytes: bundle.length, leaseId: id(9), leaseGeneration: "1" };
    } },
    runtime: new ExecutorManagedNextRuntime(executor), artifacts, isCurrent: async () => true,
  });
  const deps = { producer, loadCandidate: async () => row, custody: { store, now, retentionMs: 60_000 } };
  const load = () => loadPrivateStaticView({ store, artifacts, authority, request, bundleSha256, now });
  return { deps, load, startedStop, finishStop };
}

it.each([
  ["/series.json", "application/json", "[1,2]"],
  ["/captions.vtt", "text/vtt", "WEBVTT"],
  ["/schedule.csv", "text/csv", "box,input"],
])("publishes %s through the real build service after stop confirmation", async (path, contentType, text) => {
  const f = await fixture({ path, contentType, text });
  const { projectStaticBuilds, projectStaticPreviews } = createPrivateBuildServices(f.deps);
  try {
    await expect(projectStaticBuilds.submit(authority, request)).resolves.toMatchObject({ status: "building" });
    await f.startedStop.promise;
    expect(await f.load()).toBeNull();
  } finally {
    f.finishStop.resolve();
  }
  await vi.waitFor(async () => {
    const status = await projectStaticBuilds.observe(authority, request);
    expect(status.status).toBe("ready");
  }, { timeout: 5000 });
  const ready = await f.load();
  expect(ready).toMatchObject({ executionId, providerBuildId: "static-next:observed-build", request });
  expect(await projectStaticPreviews.read(authority, request.jobId, path))
    .toEqual({ body: Buffer.from(text), contentType });
});

it.each([
  ["cleanup", "cleanup_required"],
  ["authority", "candidate_conflict"],
  ["provider", "producer_conflict"],
] as const)("keeps %s failures out of durable custody", async (failure, code) => {
  const f = await fixture({ failure });
  f.finishStop.resolve();
  await expect(new BrowserCandidateBuildAdapter(f.deps).build(authority, request)).rejects.toMatchObject({ code });
  expect(await f.load()).toBeNull();
  expect(f.deps.producer.status(authority, request.jobId).ready).toBe(false);
});
