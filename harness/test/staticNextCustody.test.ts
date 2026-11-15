import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StaticNextObjectStore } from "../src/projectBuild/codebuildStaticNextExecutor.js";
import type { StaticNextJobResult } from "../src/projectBuild/managedNextRuntime.js";
import type { PrivateStaticArtifacts } from "../src/projectBuild/genericNextBuild.js";
import { nextRequestDigest, staticManifestDigest } from "../src/projectBuild/genericNextBuild.js";
import { DiskPrivateStaticArtifacts } from "../src/projectBuild/privateStaticArtifactStore.js";
import { STATIC_NEXT_CUSTODY_SCHEMA, loadPrivateStaticView, markStaticNextCleaned,
  publishStaticNextCustody } from "../src/projectBuild/staticNextCustody.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceRequestId: id(6), version: 1, receiptDigest: "e".repeat(64),
  sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
const scope = { ...authority, jobId: request.jobId, sourceCommit: request.sourceCommit, sourceTree: request.sourceTree };
const executionId = nextRequestDigest(scope);
const bundleSha256 = "c".repeat(64);
const bindingId = id(7);
const artifactBindingId = id(8);
const toolchainSha256 = "d".repeat(64);
const instant = new Date("2026-11-01T00:00:00.000Z");
const now = () => new Date(instant.getTime());
const hash = (v: Buffer) => createHash("sha256").update(v).digest("hex");
const base = `static-next/${bundleSha256}/${executionId}`;
const directories: string[] = [];
afterEach(async () => { await Promise.all(directories.splice(0).map(p => rm(p, { recursive: true, force: true }))); });

/** A fresh instance shares only persisted objects, never an in-process job map. */
class DiskObjects implements StaticNextObjectStore {
  constructor(readonly root: string) {}
  async get(key: string): Promise<Buffer | null> {
    try { return await readFile(join(this.root, key)); }
    catch (e) { if ((e as NodeJS.ErrnoException).code === "ENOENT") return null; throw e; }
  }
  async putIfAbsent(key: string, body: Buffer): Promise<"created" | "exists"> {
    await mkdir(dirname(join(this.root, key)), { recursive: true });
    try { await writeFile(join(this.root, key), body, { flag: "wx" }); return "created"; }
    catch (e) { if ((e as NodeJS.ErrnoException).code === "EEXIST") return "exists"; throw e; }
  }
}

async function fixture() {
  const root = await mkdtemp(join(tmpdir(), "nx3c-custody-"));
  directories.push(root);
  const store = new DiskObjects(join(root, "custody"));
  const artifactOptions = { root: join(root, "artifacts"), bindingId: artifactBindingId, isCurrent: async () => true };
  const artifacts = new DiskPrivateStaticArtifacts(artifactOptions);
  // Deliberately unsorted: custody must preserve the producer's manifest order.
  const files = [
    { path: "/index.html", body: Buffer.from("<main>durable</main>"), contentType: "text/html; charset=utf-8" },
    { path: "/_next/static/app.js", body: Buffer.from("void 0"), contentType: "text/javascript" },
  ];
  const result: StaticNextJobResult = { protocol: "leaf.static-next.v1", executionId, bindingId,
    toolchainSha256, bundleSha256, exitCode: 0, files };
  const stop = { protocol: "leaf.static-next.v1" as const, executionId, bindingId, confirmed: true };
  const publish = { store, artifacts, authority, request, result, providerBuildId: "provider:observed-123",
    stop, now, retentionMs: 60_000 };
  const load = { store, artifacts, authority, request, bundleSha256, now };
  return { root, store, artifacts, artifactOptions, files, result, publish, load };
}

describe("static Next durable custody writer and READY loader", () => {
  it("publishes the raw receipt and reopens the exact normalized view and bytes through fresh readers", async () => {
    const f = await fixture();
    const receipt = await publishStaticNextCustody(f.publish);
    const manifest = { requestDigest: executionId, bundleSha256, bindingId, toolchainSha256, files: [
      { path: "/index.html", sha256: hash(f.files[0].body), sizeBytes: f.files[0].body.length,
        contentType: "text/html; charset=utf-8" },
      { path: "/_next/static/app.js", sha256: hash(f.files[1].body), sizeBytes: f.files[1].body.length,
        contentType: "text/javascript" },
    ] };
    // The exact ID acknowledged by the artifact store is retained, never minted by custody.
    const published = await f.artifacts.put(scope, manifest, f.files);
    expect(receipt).toEqual({ schema: STATIC_NEXT_CUSTODY_SCHEMA, authority, request,
      executionId, bindingId, toolchainSha256, bundleSha256, providerBuildId: "provider:observed-123",
      terminalState: "READY", sandboxCleanupConfirmed: true, artifactBindingId,
      artifactId: published.artifactId, manifestSha256: staticManifestDigest(manifest), manifest,
      createdAt: "2026-11-01T00:00:00.000Z", cleanupAt: "2026-11-01T00:01:00.000Z" });
    const store = new DiskObjects(f.store.root);
    const artifacts = new DiskPrivateStaticArtifacts(f.artifactOptions);
    expect(JSON.parse((await store.get(`${base}/receipt.json`))!.toString())).toEqual(receipt);
    const record = await loadPrivateStaticView({ ...f.load, store, artifacts });
    expect(record).toEqual({ schema: "leaf.private-static-view.v1", authority, request, executionId,
      providerBuildId: "provider:observed-123", terminalState: "READY", sandboxCleanupConfirmed: true,
      cleaned: false, preview: { jobId: request.jobId, sourceCommit: request.sourceCommit,
        sourceTree: request.sourceTree, bundleSha256, bindingId, toolchainSha256, artifactBindingId,
        artifactId: published.artifactId, manifestSha256: published.manifestSha256, manifest } });
    for (const descriptor of record!.preview.manifest.files) {
      const asset = await artifacts.read(scope, record!.preview.artifactId, descriptor.path, 2 * 1024 * 1024);
      expect(asset.body.length).toBe(descriptor.sizeBytes);
      expect(hash(asset.body)).toBe(descriptor.sha256);
      expect(asset.contentType).toBe(descriptor.contentType);
    }
    const put = vi.spyOn(store, "putIfAbsent");
    const artifactPut = vi.spyOn(artifacts, "put");
    await loadPrivateStaticView({ ...f.load, store, artifacts });
    expect(put).not.toHaveBeenCalled();
    expect(artifactPut).not.toHaveBeenCalled();
  });

  it("accepts byte-identical publication and rejects a different receipt without overwriting", async () => {
    const f = await fixture();
    const first = await publishStaticNextCustody(f.publish);
    expect(await publishStaticNextCustody(f.publish)).toEqual(first);
    await expect(publishStaticNextCustody({ ...f.publish, providerBuildId: "another-provider-build" }))
      .rejects.toMatchObject({ code: "job_conflict" });
    await expect(publishStaticNextCustody({ ...f.publish, result: { ...f.result,
      files: [{ ...f.files[0], body: Buffer.from("different content") }] } }))
      .rejects.toMatchObject({ code: "job_conflict" });
    expect(JSON.parse((await f.store.get(`${base}/receipt.json`))!.toString())).toEqual(first);
  });

  it.each(["sourceRequestId", "version", "receiptDigest"] as const)(
    "does not relabel an execution with another candidate's %s", async key => {
      const f = await fixture();
      await publishStaticNextCustody(f.publish);
      const other = { ...request, [key]: key === "version" ? 2 : key === "receiptDigest" ? "f".repeat(64) : id(99) };
      const put = vi.spyOn(f.artifacts, "put");
      await expect(publishStaticNextCustody({ ...f.publish, request: other }))
        .rejects.toMatchObject({ code: "job_conflict" });
      expect(put).not.toHaveBeenCalled();
      expect(await loadPrivateStaticView({ ...f.load, request: other })).toBeNull();
    });

  it("enforces retention at the exact deadline using host time", async () => {
    const f = await fixture();
    await publishStaticNextCustody(f.publish);
    expect(await loadPrivateStaticView({ ...f.load, now: () => new Date(instant.getTime() + 59_999) })).not.toBeNull();
    expect(await loadPrivateStaticView({ ...f.load, now: () => new Date(instant.getTime() + 60_000) })).toBeNull();
    expect(await loadPrivateStaticView({ ...f.load, now: () => new Date(NaN) })).toBeNull();
  });

  it("rereads lifecycle state and preserves the first cleanup marker, even when filesRemoved is false", async () => {
    const f = await fixture();
    await publishStaticNextCustody(f.publish);
    expect(await loadPrivateStaticView(f.load)).not.toBeNull();
    await markStaticNextCleaned({ store: f.store, bundleSha256, executionId, filesRemoved: false, now });
    const original = await f.store.get(`${base}/cleaned.json`);
    expect(JSON.parse(original!.toString())).toEqual({ schema: STATIC_NEXT_CUSTODY_SCHEMA,
      executionId, bundleSha256, cleanedAt: instant.toISOString(), filesRemoved: false });
    await markStaticNextCleaned({ store: f.store, bundleSha256, executionId, filesRemoved: true,
      now: () => new Date(instant.getTime() + 1000) });
    expect(await f.store.get(`${base}/cleaned.json`)).toEqual(original);
    expect(await loadPrivateStaticView(f.load)).toBeNull();
  });

  it("fails closed on marker read errors and even malformed marker bytes", async () => {
    const f = await fixture();
    await publishStaticNextCustody(f.publish);
    const store: StaticNextObjectStore = { putIfAbsent: vi.fn(), get: async key => {
      if (key.endsWith("cleaned.json")) throw new Error("unreadable");
      return f.store.get(key);
    } };
    expect(await loadPrivateStaticView({ ...f.load, store })).toBeNull();
    expect(store.putIfAbsent).not.toHaveBeenCalled();
    await f.store.putIfAbsent(`${base}/cleaned.json`, Buffer.from("broken"));
    expect(await loadPrivateStaticView(f.load)).toBeNull();
  });

  it.each(["tenantId", "organizationId", "projectId", "repoKey"] as const)(
    "refuses a foreign authority's %s", async key => {
      const f = await fixture();
      await publishStaticNextCustody(f.publish);
      const foreign = { ...authority, [key]: id(99) };
      // Also put the old receipt at the foreign lookup location to exercise comparison.
      const foreignExecution = nextRequestDigest({ ...scope, ...foreign });
      await f.store.putIfAbsent(`static-next/${bundleSha256}/${foreignExecution}/receipt.json`,
        (await f.store.get(`${base}/receipt.json`))!);
      expect(await loadPrivateStaticView({ ...f.load, authority: foreign })).toBeNull();
    });

  it("refuses a different artifact binding and a different admitted bundle", async () => {
    const f = await fixture();
    await publishStaticNextCustody(f.publish);
    const artifacts = new DiskPrivateStaticArtifacts({ ...f.artifactOptions, bindingId: id(99) });
    expect(await loadPrivateStaticView({ ...f.load, artifacts })).toBeNull();
    expect(await loadPrivateStaticView({ ...f.load, bundleSha256: "f".repeat(64) })).toBeNull();
  });

  it.each(["unconfirmed", "wrong-stop-execution", "wrong-stop-binding", "failed", "wrong-execution",
    "invalid-witness", "invalid-retention", "no-files", "duplicate", "unsafe-path", "mime", "oversized-file",
    "too-many-files", "total-bytes", "metadata-bytes"])("rejects %s before either write port", async mode => {
    const f = await fixture();
    const options = { ...f.publish, result: { ...f.result }, stop: { ...f.publish.stop }, request: { ...request } };
    if (mode === "unconfirmed") options.stop.confirmed = false;
    if (mode === "wrong-stop-execution") options.stop.executionId = "f".repeat(64);
    if (mode === "wrong-stop-binding") options.stop.bindingId = id(99);
    if (mode === "failed") options.result.exitCode = 10;
    if (mode === "wrong-execution") options.result.executionId = "f".repeat(64);
    if (mode === "invalid-witness") options.request.version = 0;
    if (mode === "invalid-retention") options.retentionMs = 0;
    if (mode === "no-files") options.result.files = [];
    if (mode === "duplicate") options.result.files = [f.files[0], f.files[0]];
    if (mode === "unsafe-path") options.result.files = [{ ...f.files[0], path: "/../index.html" }];
    if (mode === "mime") options.result.files = [{ ...f.files[0], contentType: "text/html\r\nsecret" }];
    if (mode === "oversized-file") options.result.files = [{ ...f.files[0], body: Buffer.alloc(2 * 1024 * 1024 + 1) }];
    if (mode === "too-many-files") options.result.files = Array.from({ length: 4097 }, () => f.files[0]);
    if (mode === "total-bytes") options.result.files = Array.from({ length: 17 }, (_, i) => ({
      ...f.files[0], path: i === 0 ? "/index.html" : `/file-${i}`, body: Buffer.alloc(2 * 1024 * 1024),
    }));
    if (mode === "metadata-bytes") options.result.files = Array.from({ length: 1100 }, (_, i) => ({
      ...f.files[0], path: i === 0 ? "/index.html" : `/${"x".repeat(2000)}${i}`, body: Buffer.alloc(0),
    }));
    const put = vi.spyOn(f.store, "putIfAbsent");
    const artifactPut = vi.spyOn(f.artifacts, "put");
    await expect(publishStaticNextCustody(options)).rejects.toMatchObject({ code: "producer_conflict" });
    expect(put).not.toHaveBeenCalled();
    expect(artifactPut).not.toHaveBeenCalled();
  });

  it("does not publish custody if artifact acknowledgement has a mismatched digest", async () => {
    const f = await fixture();
    const artifacts: PrivateStaticArtifacts = { bindingId: artifactBindingId,
      access: "private-authenticated", immutable: true,
      put: vi.fn(async () => ({ artifactId: id(99), manifestSha256: "f".repeat(64) })), read: vi.fn() };
    const put = vi.spyOn(f.store, "putIfAbsent");
    await expect(publishStaticNextCustody({ ...f.publish, artifacts })).rejects.toMatchObject({ code: "producer_conflict" });
    expect(artifacts.put).toHaveBeenCalledOnce();
    expect(put).not.toHaveBeenCalled();
    expect(await loadPrivateStaticView(f.load)).toBeNull();
  });

  it("publishes receipt only after complete artifact acknowledgement", async () => {
    const f = await fixture();
    let acknowledge!: () => void;
    let entered!: () => void;
    const reached = new Promise<void>(resolve => { entered = resolve; });
    const barrier = new Promise<void>(resolve => { acknowledge = resolve; });
    const artifacts: PrivateStaticArtifacts = { bindingId: artifactBindingId,
      access: "private-authenticated", immutable: true, read: f.artifacts.read.bind(f.artifacts),
      put: async (...args) => { const saved = await f.artifacts.put(...args); entered(); await barrier; return saved; } };
    const pending = publishStaticNextCustody({ ...f.publish, artifacts });
    await reached;
    expect(await loadPrivateStaticView(f.load)).toBeNull();
    acknowledge();
    await pending;
    expect(await loadPrivateStaticView(f.load)).not.toBeNull();
  });

  it.each(["missing", "unreadable", "unparseable", "oversized", "manifest", "not-ready", "cleanup-fact", "timestamp"])(
    "returns null for %s custody without repairing it", async mode => {
      const f = await fixture();
      const receipt = await publishStaticNextCustody(f.publish);
      let raw: Buffer | null = Buffer.from(JSON.stringify(receipt));
      if (mode === "missing") raw = null;
      if (mode === "unparseable") raw = Buffer.from("{");
      if (mode === "oversized") raw = Buffer.alloc(2 * 1024 * 1024 + 1);
      if (mode === "manifest") raw = Buffer.from(JSON.stringify({ ...receipt, manifestSha256: "f".repeat(64) }));
      if (mode === "not-ready") raw = Buffer.from(JSON.stringify({ ...receipt, terminalState: "FAILED" }));
      if (mode === "cleanup-fact") raw = Buffer.from(JSON.stringify({ ...receipt, sandboxCleanupConfirmed: false }));
      if (mode === "timestamp") raw = Buffer.from(JSON.stringify({ ...receipt, cleanupAt: "invalid" }));
      const store: StaticNextObjectStore = { putIfAbsent: vi.fn(), get: async () => {
        if (mode === "unreadable") throw new Error("unreadable receipt");
        return raw;
      } };
      expect(await loadPrivateStaticView({ ...f.load, store })).toBeNull();
      expect(store.putIfAbsent).not.toHaveBeenCalled();
    });
});
