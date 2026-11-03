import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { GenericNextProducer, NEXT_PLAN, nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { ManagedNextRuntime, NextBuildScope } from "../src/projectBuild/genericNextBuild.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
const bundle = Buffer.from("test-only immutable Git bundle fixture, not a real Next runtime");
const bundleSha256 = createHash("sha256").update(bundle).digest("hex");

function fixture() {
  let scope: NextBuildScope;
  const lease = {
    build: vi.fn(async () => ({ requestDigest: nextRequestDigest(scope), previewId: id(6), bundleSha256 })),
    read: vi.fn(async () => ({ body: Buffer.from("<main>private fixture</main>"), contentType: "text/html" })),
    destroy: vi.fn(async () => undefined),
  };
  const runtime: ManagedNextRuntime = {
    bindingId: id(7), toolchainSha256: "d".repeat(64),
    isolation: "filesystem-and-network", ingress: "private-authenticated",
    create: vi.fn(async s => { scope = s; return lease; }),
  };
  const repository = { exportProjectSourceBundle: vi.fn(async () => ({
    ...request, bundle: Buffer.from(bundle), bundleSha256, sizeBytes: bundle.length,
    leaseId: id(8), leaseGeneration: "1",
  })) };
  const isCurrent = vi.fn(async () => true);
  const deps = { runtime, repository, isCurrent };
  return { ...deps, deps, lease, producer: new GenericNextProducer(deps) };
}

describe("GenericNextProducer source-level contract (mocked resource, no isolation proof)", () => {
  it("connects immutable source through fixed plan and scoped private read, then releases", async () => {
    const f = fixture();
    const preview = await f.producer.build(authority, request);
    expect(f.repository.exportProjectSourceBundle).toHaveBeenCalledWith(authority, {
      sourceCommit: request.sourceCommit, sourceTree: request.sourceTree,
    });
    expect(f.lease.build).toHaveBeenCalledWith(bundle, bundleSha256, NEXT_PLAN);
    expect(NEXT_PLAN.build).not.toContain("npm");
    expect(NEXT_PLAN.network).toBe("deny");
    expect(Object.isFrozen(NEXT_PLAN.env)).toBe(true);
    expect(preview).toMatchObject({ ...request, previewId: id(6), bindingId: id(7), bundleSha256 });
    expect((await f.producer.read(authority, request.jobId)).body.toString()).toContain("private fixture");
    expect(f.lease.read).toHaveBeenCalledWith(id(6), "/", 2 * 1024 * 1024);
    expect(f.producer.lastValid(authority)).toEqual(preview);
    await f.producer.release(authority, request.jobId);
    expect(f.lease.destroy).toHaveBeenCalledOnce();
    expect(f.producer.lastValid(authority)).toBeUndefined();
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
    expect(() => f.producer.build(authority, request)).toThrow("preview_released");
  });

  it("refuses missing execution binding before exporting source", () => {
    const f = fixture();
    const p = new GenericNextProducer({ repository: f.repository, isCurrent: f.isCurrent });
    expect(() => p.build(authority, request)).toThrow("isolated_next_binding_unavailable");
    expect(f.repository.exportProjectSourceBundle).not.toHaveBeenCalled();
  });

  it("joins identical in-flight jobs and refuses a changed immutable source under same job", async () => {
    const f = fixture();
    const one = f.producer.build(authority, request);
    expect(f.producer.build(authority, request)).toBe(one);
    expect(() => f.producer.build(authority, { ...request, sourceTree: "c".repeat(40) })).toThrow("job_conflict");
    await one;
    expect(f.runtime.create).toHaveBeenCalledOnce();
  });

  it("does not share jobs or previews across tenant, org, project or repository", async () => {
    const f = fixture();
    await f.producer.build(authority, request);
    for (const key of Object.keys(authority)) {
      const foreign = { ...authority, [key]: id(99) };
      await expect(f.producer.read(foreign, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
      await expect(f.producer.release(foreign, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
    }
    expect(f.lease.read).not.toHaveBeenCalled();
    expect(f.lease.destroy).not.toHaveBeenCalled();
    await f.producer.release(authority, request.jobId);
  });

  it.each(["sourceCommit", "sourceTree", "bundleSha256", "sizeBytes", "leaseId", "leaseGeneration"])(
    "rejects corrupted source field %s before creating a resource", async field => {
      const f = fixture();
      const good = await f.repository.exportProjectSourceBundle();
      f.repository.exportProjectSourceBundle.mockResolvedValue({ ...good,
        [field]: field === "sizeBytes" ? good.sizeBytes + 1 : "invalid" });
      await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_conflict" });
      expect(f.runtime.create).not.toHaveBeenCalled();
    });

  it("rejects altered bundle bytes even with matching size", async () => {
    const f = fixture();
    const good = await f.repository.exportProjectSourceBundle();
    good.bundle[0] ^= 1;
    f.repository.exportProjectSourceBundle.mockResolvedValue(good);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_conflict" });
    expect(f.runtime.create).not.toHaveBeenCalled();
  });

  it("refuses stale source before export", async () => {
    const f = fixture(); f.isCurrent.mockResolvedValue(false);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_stale" });
    expect(f.repository.exportProjectSourceBundle).not.toHaveBeenCalled();
  });

  it("destroys a late build after scope/source switch without publishing it", async () => {
    const f = fixture();
    f.isCurrent.mockResolvedValueOnce(true).mockResolvedValueOnce(true).mockResolvedValue(false);
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "source_stale" });
    expect(f.lease.destroy).toHaveBeenCalledOnce();
    expect(f.producer.lastValid(authority)).toBeUndefined();
  });

  it("rejects a foreign producer receipt and cleans the isolated lease", async () => {
    const f = fixture();
    f.lease.build.mockResolvedValue({ requestDigest: "x", previewId: id(6), bundleSha256 });
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "producer_conflict" });
    expect(f.lease.destroy).toHaveBeenCalledOnce();
  });

  it("retains the last valid preview when an edited build fails", async () => {
    const f = fixture();
    const before = await f.producer.build(authority, request);
    const source = await f.repository.exportProjectSourceBundle();
    f.repository.exportProjectSourceBundle.mockResolvedValue({ ...source, sourceCommit: "c".repeat(40) });
    f.lease.build.mockRejectedValue(new Error("private provider detail"));
    await expect(f.producer.build(authority, { ...request, jobId: id(9), sourceCommit: "c".repeat(40) }))
      .rejects.toMatchObject({ code: "build_failed" });
    expect(f.runtime.create).toHaveBeenCalledTimes(2);
    expect(f.lease.destroy).toHaveBeenCalledOnce();
    expect(f.producer.lastValid(authority)).toEqual(before);
    await f.producer.release(authority, request.jobId);
  });

  it("hides current-source lookup failure details during private read", async () => {
    const f = fixture(); await f.producer.build(authority, request);
    f.isCurrent.mockRejectedValue(new Error("private authority transport detail"));
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "source_unavailable" });
    expect(f.lease.read).not.toHaveBeenCalled();
    await f.producer.release(authority, request.jobId);
  });

  it("keeps a failed cleanup lease retryable without exposing provider error", async () => {
    const f = fixture();
    f.lease.build.mockRejectedValue(new Error("private provider detail"));
    f.lease.destroy.mockRejectedValueOnce(new Error("private cleanup detail"));
    await expect(f.producer.build(authority, request)).rejects.toMatchObject({ code: "cleanup_required" });
    await f.producer.release(authority, request.jobId);
    expect(f.lease.destroy).toHaveBeenCalledTimes(2);
  });

  it("does not return a late private read after a source switch", async () => {
    const f = fixture(); await f.producer.build(authority, request);
    f.isCurrent.mockResolvedValueOnce(true).mockResolvedValue(false);
    await expect(f.producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "source_stale" });
    await f.producer.release(authority, request.jobId);
  });

  it.each(["//foreign.test", "https://foreign.test/", "/../secret", "/%2e%2e/", "/x\\secret", "/x\nheader"])(
    "refuses unsafe preview path %s", async path => {
      const f = fixture(); await f.producer.build(authority, request);
      await expect(f.producer.read(authority, request.jobId, path)).rejects.toMatchObject({ code: "preview_path_invalid" });
      expect(f.lease.read).not.toHaveBeenCalled();
      await f.producer.release(authority, request.jobId);
    });
});
