import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { GenericNextProducer, NEXT_PLAN, nextRequestDigest, staticManifestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { ManagedNextRuntime, NextBuildScope, PrivateStaticArtifacts, StaticFile } from "../src/projectBuild/genericNextBuild.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
const bundle = Buffer.from("immutable Git bundle fixture, not runtime proof");
const bundleSha256 = createHash("sha256").update(bundle).digest("hex");
const body = Buffer.from("<main>private fixture</main>");

function fixture() {
  let scope: NextBuildScope;
  const files: StaticFile[] = [{ path: "/index.html", body: Buffer.from(body), contentType: "text/html" }];
  const lease = {
    build: vi.fn(async () => ({ requestDigest: nextRequestDigest(scope), bundleSha256, files })),
    destroy: vi.fn(async () => ({ confirmed: true })),
  };
  const runtime: ManagedNextRuntime = {
    bindingId: id(7), toolchainSha256: "d".repeat(64), isolation: "filesystem-and-network",
    create: vi.fn(async s => { scope = s; return lease; }),
  };
  const artifacts: PrivateStaticArtifacts = {
    bindingId: id(8), access: "private-authenticated", immutable: true,
    put: vi.fn(async (_s, manifest) => ({ artifactId: id(6), manifestSha256: staticManifestDigest(manifest) })),
    read: vi.fn(async () => ({ body: Buffer.from(body), contentType: "text/html" })),
  };
  const repository = { exportProjectSourceBundle: vi.fn(async () => ({
    ...request, bundle: Buffer.from(bundle), bundleSha256, sizeBytes: bundle.length,
    leaseId: id(9), leaseGeneration: "1",
  })) };
  const isCurrent = vi.fn(async () => true);
  const deps = { runtime, artifacts, repository, isCurrent };
  return { ...deps, deps, lease, files, producer: new GenericNextProducer(deps) };
}

describe("GenericNextProducer static contract (mocked adapters, no runtime proof)", () => {
  it("exports immutable assets and confirms sandbox cleanup before publishing", async () => {
    const f = fixture();
    const preview = await f.producer.build(authority, request);
    expect(f.repository.exportProjectSourceBundle).toHaveBeenCalledWith(authority, {
      sourceCommit: request.sourceCommit, sourceTree: request.sourceTree,
    });
    expect(f.lease.build).toHaveBeenCalledWith(bundle, bundleSha256, NEXT_PLAN);
    expect(NEXT_PLAN).not.toHaveProperty("serve");
    expect(NEXT_PLAN).not.toHaveProperty("previewTtlMs");
    expect(NEXT_PLAN.output).toBe("out");
    expect(NEXT_PLAN.network).toBe("deny");
    expect(preview).toMatchObject({ ...request, artifactId: id(6), bundleSha256 });
    expect(preview.manifestSha256).toBe(staticManifestDigest(preview.manifest));
    expect(Object.isFrozen(preview.manifest.files[0])).toBe(true);
    expect(f.producer.status(authority, request.jobId)).toEqual({ ready: true, cleanup: "confirmed" });
    expect(f.lease.destroy).toHaveBeenCalledOnce();
    expect((await f.producer.read(authority, request.jobId)).body).toEqual(body);
    expect(f.artifacts.read).toHaveBeenCalledWith(expect.objectContaining(authority), id(6), "/index.html", body.length);
    await f.producer.release(authority, request.jobId);
    expect(f.lease.destroy).toHaveBeenCalledOnce();
    expect(f.producer.lastValid(authority)).toBeUndefined();
    expect(() => f.producer.build(authority, request)).toThrow("preview_released");
  });

  it.each(["runtime", "artifacts"] as const)("fails closed without %s before source export", key => {
    const f = fixture();
    const producer = new GenericNextProducer({ ...f.deps, [key]: undefined });
    expect(() => producer.build(authority, request)).toThrow(
      key === "runtime" ? "isolated_next_binding_unavailable" : "private_artifact_binding_unavailable");
    expect(f.repository.exportProjectSourceBundle).not.toHaveBeenCalled();
  });

  it("refuses unverified supply and public or mutable artifact bindings", () => {
    const f = fixture();
    Object.assign(f.runtime, { toolchainSha256: "" });
    expect(() => f.producer.build(authority, request)).toThrow("isolated_next_binding_unavailable");
    const g = fixture();
    Object.assign(g.artifacts, { access: "public", immutable: false });
    expect(() => g.producer.build(authority, request)).toThrow("private_artifact_binding_unavailable");
  });

  it("joins the same job and rejects changed source", async () => {
    const f = fixture();
    const one = f.producer.build(authority, request);
    expect(f.producer.build(authority, request)).toBe(one);
    expect(() => f.producer.build(authority, { ...request, sourceTree: "c".repeat(40) })).toThrow("job_conflict");
    await one;
    expect(f.runtime.create).toHaveBeenCalledOnce();
  });

  it("keeps authority separate across tenants, organizations, projects and repositories", async () => {
    const f = fixture(); await f.producer.build(authority, request);
    for (const key of Object.keys(authority)) {
      const foreign = { ...authority, [key]: id(99) };
      await expect(f.producer.read(foreign, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
      await expect(f.producer.release(foreign, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
    }
    expect(f.artifacts.read).not.toHaveBeenCalled();
  });

  it.each(["sourceCommit", "sourceTree", "bundleSha256", "sizeBytes", "leaseId", "leaseGeneration"])(
    "rejects invalid source %s before resource creation", async field => {
      const f = fixture();
      const source = await f.repository.exportProjectSourceBundle();
      f.repository.exportProjectSourceBundle.mockResolvedValue({ ...source, [field]: field === "sizeBytes" ? 0 : "invalid" });
      await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_conflict" });
      expect(f.runtime.create).not.toHaveBeenCalled();
    });

  it("detects altered source bytes", async () => {
    const f = fixture(); const source = await f.repository.exportProjectSourceBundle();
    source.bundle[0] ^= 1;
    f.repository.exportProjectSourceBundle.mockResolvedValue(source);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_conflict" });
  });

  it("refuses stale source before export", async () => {
    const f = fixture(); f.isCurrent.mockResolvedValue(false);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_stale" });
    expect(f.repository.exportProjectSourceBundle).not.toHaveBeenCalled();
  });

  it.each(["/../secret", "/%2e%2e/x", "//other", "/a\\b", "/a\nheader"])(
    "rejects unsafe exported path %s and cleans up", async path => {
      const f = fixture(); f.files[0] = { ...f.files[0], path };
      await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "artifact_conflict" });
      expect(f.artifacts.put).not.toHaveBeenCalled();
      expect(f.lease.destroy).toHaveBeenCalledOnce();
    });

  it("rejects duplicate assets and oversized files", async () => {
    const f = fixture(); f.files.push(f.files[0]);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "artifact_conflict" });
    const g = fixture(); g.files[0] = { ...g.files[0], body: Buffer.alloc(2 * 1024 * 1024 + 1) };
    await expect(g.producer.build(authority, request)).rejects.toMatchObject({ code: "artifact_conflict" });
  });

  it("rejects producer and custody digest conflicts", async () => {
    const f = fixture();
    f.lease.build.mockResolvedValue({ requestDigest: "x", bundleSha256, files: f.files });
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "producer_conflict" });
    const g = fixture();
    vi.mocked(g.artifacts.put).mockResolvedValue({ artifactId: id(6), manifestSha256: "f".repeat(64) });
    await expect(g.producer.build(authority, request)).rejects.toMatchObject({ code: "artifact_conflict" });
    expect(g.lease.destroy).toHaveBeenCalledOnce();
  });

  it.each([false, undefined])("keeps unconfirmed cleanup %s pending without readiness", async confirmed => {
    const f = fixture();
    f.lease.destroy.mockResolvedValueOnce({ confirmed: confirmed as boolean });
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "cleanup_required" });
    expect(f.producer.status(authority, request.jobId)).toEqual({ cleanup: "pending", ready: false });
    expect(f.producer.lastValid(authority)).toBeUndefined();
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
    await f.producer.release(authority, request.jobId);
    expect(f.lease.destroy).toHaveBeenCalledTimes(2);
  });

  it("retains cleanup failures for retry and hides provider details", async () => {
    const f = fixture(); f.lease.build.mockRejectedValue(new Error("private provider detail"));
    f.lease.destroy.mockRejectedValueOnce(new Error("private cleanup detail"));
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "cleanup_required" });
    expect(f.producer.status(authority, request.jobId).cleanup).toBe("pending");
    await f.producer.release(authority, request.jobId);
    expect(f.lease.destroy).toHaveBeenCalledTimes(2);
  });

  it("rejects altered private asset bytes and content type", async () => {
    const f = fixture(); await f.producer.build(authority, request);
    vi.mocked(f.artifacts.read).mockResolvedValueOnce({ body: Buffer.alloc(body.length), contentType: "text/html" });
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "preview_response_invalid" });
    vi.mocked(f.artifacts.read).mockResolvedValueOnce({ body, contentType: "text/plain" });
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "preview_response_invalid" });
  });

  it("rejects a late read after a source switch", async () => {
    const f = fixture(); await f.producer.build(authority, request);
    f.isCurrent.mockResolvedValueOnce(true).mockResolvedValue(false);
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "source_stale" });
  });

  it("rejects stale source after export and preserves the last successful artifact on later failure", async () => {
    const f = fixture(); const before = await f.producer.build(authority, request);
    f.isCurrent.mockResolvedValueOnce(true).mockResolvedValueOnce(true).mockResolvedValue(false);
    await expect(f.producer.build(authority, { ...request, jobId: id(10) })).rejects.toMatchObject({ code: "source_stale" });
    expect(f.producer.lastValid(authority)).toEqual(before);
    expect(f.lease.destroy).toHaveBeenCalledTimes(2);
  });
});
