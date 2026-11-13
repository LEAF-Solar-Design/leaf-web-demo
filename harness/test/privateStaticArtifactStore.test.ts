import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, rename, rm, symlink, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { nextRequestDigest, staticManifestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { NextBuildScope, StaticFile, StaticManifest } from "../src/projectBuild/genericNextBuild.js";
import { DiskPrivateStaticArtifacts } from "../src/projectBuild/privateStaticArtifactStore.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const scope: NextBuildScope = { tenantId: id(1), organizationId: id(2), projectId: id(3),
  repoKey: id(4), jobId: id(5), sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
const bindingId = id(8);
const hash = (v: Buffer | string) => createHash("sha256").update(v).digest("hex");
const directories: string[] = [];
afterEach(async () => { await Promise.all(directories.splice(0).map(p => rm(p, { recursive: true, force: true }))); });

function data(s = scope, text = "<main>stored, never executed</main>") {
  const files: StaticFile[] = [
    { path: "/index.html", body: Buffer.from(text), contentType: "text/html; charset=utf-8" },
    { path: "/_next/static/chunk.js", body: Buffer.from("throw new Error('do not execute')"), contentType: "text/javascript" },
  ];
  const manifest: StaticManifest = { requestDigest: nextRequestDigest(s), bundleSha256: "c".repeat(64),
    bindingId: id(7), toolchainSha256: "d".repeat(64), files: files.map(f => ({
      path: f.path, sizeBytes: f.body.length, sha256: hash(f.body), contentType: f.contentType,
    })) };
  return { manifest, files };
}

async function fixture(isCurrent: (s: NextBuildScope) => Promise<boolean> = async () => true) {
  const jail = await mkdtemp(join(tmpdir(), "b03-private-static-"));
  directories.push(jail);
  const root = join(jail, "private");
  const options = { root, bindingId, isCurrent };
  const store = new DiskPrivateStaticArtifacts(options);
  const location = (s = scope) => join(root, bindingId, nextRequestDigest(s));
  const asset = (path = "/index.html", s = scope) => join(location(s), hash(path) + ".asset");
  return { jail, root, options, store, location, asset, ...data() };
}

describe("DiskPrivateStaticArtifacts real disk custody, no runtime or HTTP serving", () => {
  it("publishes immutable bytes and reopens them through a new instance", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    expect(saved.manifestSha256).toBe(staticManifestDigest(f.manifest));
    const reopened = new DiskPrivateStaticArtifacts(f.options);
    for (const file of f.files) {
      const result = await reopened.read(scope, saved.artifactId, file.path, file.body.length);
      expect(result).toEqual({ body: file.body, contentType: file.contentType });
    }
    expect((await reopened.read(scope, saved.artifactId, "/", 1024)).body).toEqual(f.files[0].body);
    expect(await readdir(join(f.root, bindingId))).toEqual([nextRequestDigest(scope)]);
  });

  it("makes an exact repeated put idempotent across instances", async () => {
    const f = await fixture();
    const one = await f.store.put(scope, f.manifest, f.files);
    const bytes = await readFile(join(f.location(), "manifest.json"));
    expect(await new DiskPrivateStaticArtifacts(f.options).put(scope, f.manifest, [...f.files].reverse())).toEqual(one);
    expect(await readFile(join(f.location(), "manifest.json"))).toEqual(bytes);
  });

  it("rejects different bytes under an existing immutable identity", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const changed = data(scope, "<main>changed</main>");
    await expect(f.store.put(scope, changed.manifest, changed.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    expect((await f.store.read(scope, saved.artifactId, "/", 1024)).body).toEqual(f.files[0].body);
  });

  it("snapshots scope, manifest and byte buffers before async authorization", async () => {
    let unblock!: (v: boolean) => void;
    let first = true;
    const f = await fixture(async () => first ? (first = false, new Promise<boolean>(r => { unblock = r; })) : true);
    const mutableScope = { ...scope };
    const input = data();
    const pending = f.store.put(mutableScope, input.manifest, input.files);
    mutableScope.projectId = id(99);
    input.files[0].body.fill(0);
    (input.manifest.files[0] as { contentType: string }).contentType = "invalid";
    unblock(true);
    const saved = await pending;
    expect((await f.store.read(scope, saved.artifactId, "/", 1024)).body).toEqual(f.files[0].body);
  });

  it("does not publish a manifest after authorization is revoked during staging", async () => {
    let calls = 0;
    const f = await fixture(async () => ++calls < 2);
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toMatchObject({ code: "source_stale" });
    expect(await readdir(join(f.root, bindingId))).toEqual([]);
  });

  it("leaves interrupted staging invisible and permits a fresh complete publication", async () => {
    const f = await fixture();
    const abandoned = join(f.root, bindingId, ".interrupted.pending");
    await mkdir(abandoned, { recursive: true });
    await writeFile(join(abandoned, hash("/index.html") + ".asset"), "partial");
    const saved = await f.store.put(scope, f.manifest, f.files);
    expect((await f.store.read(scope, saved.artifactId, "/", 1024)).body).toEqual(f.files[0].body);
    expect(await readFile(join(abandoned, hash("/index.html") + ".asset"), "utf8")).toBe("partial");
  });

  it("does not expose bytes before the manifest commit and atomic publication", async () => {
    let calls = 0;
    let staged!: () => void;
    let resume!: (v: boolean) => void;
    const reached = new Promise<void>(r => { staged = r; });
    const f = await fixture(async () => {
      if (++calls === 2) { staged(); return new Promise<boolean>(r => { resume = r; }); }
      return true;
    });
    const writing = f.store.put(scope, f.manifest, f.files);
    await reached;
    const names = await readdir(join(f.root, bindingId));
    expect(names).toHaveLength(1);
    expect(names[0]).toMatch(/\.pending$/);
    expect(await readdir(join(f.root, bindingId, names[0]))).not.toContain("manifest.json");
    const provisional = staticManifestDigest(f.manifest);
    const provisionalId = `${provisional.slice(0, 8)}-${provisional.slice(8, 12)}-${provisional.slice(12, 16)}-${provisional.slice(16, 20)}-${provisional.slice(20, 32)}`;
    await expect(f.store.read(scope, provisionalId, "/", 1024)).rejects.toThrow();
    resume(true);
    const saved = await writing;
    expect((await f.store.read(scope, saved.artifactId, "/", 1024)).body).toEqual(f.files[0].body);
  });

  it("converges concurrent identical publications from separate store instances", async () => {
    const f = await fixture();
    const saved = await Promise.all(Array.from({ length: 8 }, () =>
      new DiskPrivateStaticArtifacts(f.options).put(scope, f.manifest, f.files)));
    expect(new Set(saved.map(s => JSON.stringify(s))).size).toBe(1);
    expect(await readdir(join(f.root, bindingId))).toEqual([nextRequestDigest(scope)]);
    expect((await f.store.read(scope, saved[0].artifactId, "/", 1024)).body).toEqual(f.files[0].body);
  });

  it("allows exactly one winner for concurrent conflicting publications", async () => {
    const f = await fixture();
    const other = data(scope, "other immutable content");
    const results = await Promise.allSettled([
      f.store.put(scope, f.manifest, f.files),
      new DiskPrivateStaticArtifacts(f.options).put(scope, other.manifest, other.files),
    ]);
    expect(results.filter(x => x.status === "fulfilled")).toHaveLength(1);
    const index = results.findIndex(x => x.status === "fulfilled");
    const winner = results[index] as PromiseFulfilledResult<{ artifactId: string }>;
    expect((await f.store.read(scope, winner.value.artifactId, "/", 1024)).body)
      .toEqual(index === 0 ? f.files[0].body : other.files[0].body);
  });

  it.each(["tenantId", "organizationId", "projectId", "repoKey", "jobId", "sourceCommit", "sourceTree"] as const)(
    "isolates the stored artifact from a foreign %s", async key => {
      const f = await fixture();
      const saved = await f.store.put(scope, f.manifest, f.files);
      const foreign = { ...scope, [key]: key.startsWith("source") ? "e".repeat(40) : id(99) };
      await expect(f.store.read(foreign, saved.artifactId, "/", 1024))
        .rejects.toMatchObject({ code: "preview_unavailable" });
    });

  it("does not share custody across artifact bindings", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const other = new DiskPrivateStaticArtifacts({ ...f.options, bindingId: id(99) });
    await expect(other.read(scope, saved.artifactId, "/", 1024)).rejects.toThrow();
  });

  it("rechecks revoked authorization after a disk read and returns no bytes", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    let checks = 0;
    const revoked = new DiskPrivateStaticArtifacts({ ...f.options, isCurrent: async () => ++checks < 3 });
    await expect(revoked.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "source_stale" });
    expect(checks).toBe(3);
  });

  it("fails closed when the authorization service throws", async () => {
    const f = await fixture(async () => { throw new Error("private authorization detail"); });
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toMatchObject({ code: "source_unavailable" });
    await expect(readdir(f.root)).rejects.toThrow();
  });

  it("requires an actual true authorization result rather than a truthy value", async () => {
    const f = await fixture(async () => "false" as unknown as boolean);
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toMatchObject({ code: "source_stale" });
    await expect(readdir(f.root)).rejects.toThrow();
  });

  it("detects altered persisted bytes on read and idempotent put", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    await writeFile(f.asset(), Buffer.alloc(f.files[0].body.length, 120));
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toMatchObject({ code: "artifact_conflict" });
  });

  it.each(["manifest.json", "asset"])("rejects a missing committed %s", async name => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    await unlink(name === "asset" ? f.asset() : join(f.location(), name));
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toThrow();
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toThrow();
  });

  it.each(["scope", "manifestSha256", "manifest"])("rejects corrupted persisted %s", async field => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const file = join(f.location(), "manifest.json");
    const row = JSON.parse(await readFile(file, "utf8"));
    if (field === "scope") row.scope.projectId = id(99);
    else if (field === "manifest") row.manifest.files[0].sha256 = "e".repeat(64);
    else row.manifestSha256 = "e".repeat(64);
    await writeFile(file, JSON.stringify(row));
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
  });

  it("rejects a malformed manifest rather than interpreting content", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    await writeFile(join(f.location(), "manifest.json"), "{not-json");
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toThrow();
  });

  it("binds the requested artifact ID to the original manifest after reopen", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const file = join(f.location(), "manifest.json");
    const row = JSON.parse(await readFile(file, "utf8"));
    const replacement = Buffer.from("replacement");
    row.manifest.files[0].sha256 = hash(replacement);
    row.manifest.files[0].sizeBytes = replacement.length;
    row.manifestSha256 = staticManifestDigest(row.manifest);
    await writeFile(f.asset(), replacement);
    await writeFile(file, JSON.stringify(row));
    const reopened = new DiskPrivateStaticArtifacts(f.options);
    await expect(reopened.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "preview_unavailable" });
  });

  it("rejects total-byte and file-count limits before writing disk state", async () => {
    const f = await fixture();
    const body = Buffer.alloc(2 * 1024 * 1024);
    const files = Array.from({ length: 17 }, (_, i) => ({
      path: i === 0 ? "/index.html" : `/asset-${i}.bin`, body, contentType: "application/octet-stream",
    }));
    const manifest: StaticManifest = { ...f.manifest, files: files.map(x => ({
      path: x.path, sha256: hash(x.body), sizeBytes: x.body.length, contentType: x.contentType,
    })) };
    await expect(f.store.put(scope, manifest, files)).rejects.toMatchObject({ code: "artifact_conflict" });
    const tooMany: StaticManifest = { ...f.manifest, files: Array.from({ length: 4097 }, () => f.manifest.files[0]) };
    await expect(f.store.put(scope, tooMany, f.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    await expect(readdir(f.root)).rejects.toThrow();
  });

  it("rejects an oversized disk manifest and oversized stored asset", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    await writeFile(f.asset(), Buffer.alloc(2 * 1024 * 1024 + 1));
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
    await writeFile(join(f.location(), "manifest.json"), Buffer.alloc(16 * 1024 * 1024 + 1));
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
  });

  it.each(["/../outside", "/nested/../outside", "//outside", "/a%2fb", "/a\\b", "/a?token=x", "/a#x"])(
    "rejects unsafe asset path %s", async path => {
      const f = await fixture();
      const saved = await f.store.put(scope, f.manifest, f.files);
      await expect(f.store.read(scope, saved.artifactId, path, 1024)).rejects.toMatchObject({ code: "preview_path_invalid" });
      const bad = data();
      (bad.manifest.files[0] as { path: string }).path = path;
      await expect(f.store.put(scope, bad.manifest, bad.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    });

  it.each([-1, 0, Number.NaN, Number.POSITIVE_INFINITY, 2 * 1024 * 1024 + 1])(
    "refuses invalid or insufficient read limit %s", async limit => {
      const f = await fixture();
      const saved = await f.store.put(scope, f.manifest, f.files);
      await expect(f.store.read(scope, saved.artifactId, "/", limit)).rejects.toMatchObject({ code: "artifact_conflict" });
    });

  it("checks manifest, file bytes, media types and limits before disk publication", async () => {
    const f = await fixture();
    const invalid = [
      { ...f.manifest, requestDigest: "e".repeat(64) },
      { ...f.manifest, files: [] },
      { ...f.manifest, files: [...f.manifest.files, f.manifest.files[0]] },
      { ...f.manifest, files: f.manifest.files.map(x => ({ ...x, sizeBytes: 2 * 1024 * 1024 + 1 })) },
      { ...f.manifest, files: f.manifest.files.map(x => ({ ...x, contentType: "text/html\r\nsecret" })) },
      { ...f.manifest, files: f.manifest.files.slice(1) },
    ];
    for (const m of invalid) await expect(f.store.put(scope, m, f.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    const altered = data(); altered.files[0].body.fill(0);
    await expect(f.store.put(scope, altered.manifest, altered.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    await expect(readdir(f.root)).rejects.toThrow();
  });

  it("rejects roots that are relative or the filesystem root", async () => {
    const f = await fixture();
    expect(() => new DiskPrivateStaticArtifacts({ ...f.options, root: "relative" })).toThrow();
    expect(() => new DiskPrivateStaticArtifacts({ ...f.options, root: process.platform === "win32" ? "C:\\" : "/" })).toThrow();
  });

  it("rejects a symlink storage root without reading or writing its target", async () => {
    const f = await fixture();
    const outside = join(f.jail, "outside");
    await mkdir(outside);
    await symlink(outside, f.root, process.platform === "win32" ? "junction" : "dir");
    await expect(f.store.put(scope, f.manifest, f.files)).rejects.toMatchObject({ code: "artifact_conflict" });
    expect(await readdir(outside)).toEqual([]);
  });

  it("rejects a symlink in the committed artifact directory", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const outside = join(f.jail, "outside");
    await rename(f.location(), outside);
    await symlink(outside, f.location(), process.platform === "win32" ? "junction" : "dir");
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
  });

  it("rejects an asset symlink even when its target has the expected bytes", async () => {
    const f = await fixture();
    const saved = await f.store.put(scope, f.manifest, f.files);
    const outside = join(f.jail, "outside.txt");
    await writeFile(outside, f.files[0].body);
    await unlink(f.asset());
    await symlink(outside, f.asset(), "file");
    await expect(f.store.read(scope, saved.artifactId, "/", 1024)).rejects.toMatchObject({ code: "artifact_conflict" });
  });
});
