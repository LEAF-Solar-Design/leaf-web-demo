import { describe, expect, it, vi } from "vitest";
import { BrowserCandidateBuildAdapter } from "../src/projectBuild/browserCandidateBuild.js";
import type { BrowserCandidateBuildRequest, PublishedBrowserCandidate } from "../src/projectBuild/browserCandidateBuild.js";
import { nextRequestDigest, staticManifestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { GenericNextProducer, NextPreview } from "../src/projectBuild/genericNextBuild.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const witness = { sourceRequestId: id(5), version: 2, receiptDigest: "a".repeat(64),
  sourceCommit: "b".repeat(40), sourceTree: "c".repeat(40) };
const request: BrowserCandidateBuildRequest = { ...witness, jobId: id(6) };
function fixture() {
  const row: PublishedBrowserCandidate = { ...witness, schema: "leaf.browser-project-source.v1", state: "published", authority: { ...authority } };
  const manifest = { requestDigest: nextRequestDigest({ ...authority, jobId: request.jobId, sourceCommit: witness.sourceCommit, sourceTree: witness.sourceTree }),
    bundleSha256: "d".repeat(64), bindingId: id(7), toolchainSha256: "e".repeat(64),
    files: [{ path: "/index.html", sha256: "f".repeat(64), sizeBytes: 20, contentType: "text/html" }] };
  const preview: NextPreview = { jobId: request.jobId, sourceCommit: witness.sourceCommit, sourceTree: witness.sourceTree,
    bundleSha256: manifest.bundleSha256, bindingId: manifest.bindingId, toolchainSha256: manifest.toolchainSha256,
    artifactBindingId: id(8), artifactId: id(9), manifestSha256: staticManifestDigest(manifest), manifest };
  const producer = { build: vi.fn<GenericNextProducer["build"]>(async () => preview),
    status: vi.fn<GenericNextProducer["status"]>(() => ({ ready: true, cleanup: "confirmed" })) };
  const loadCandidate = vi.fn(async (): Promise<PublishedBrowserCandidate | null> => row);
  return { row, preview, producer, loadCandidate, adapter: new BrowserCandidateBuildAdapter({ producer, loadCandidate }) };
}

describe("B01 candidate identity adapter (fake producer, no runtime proof)", () => {
  it("uses the exact generic interface and returns candidate-bound terminal evidence", async () => {
    const f = fixture();
    const result = await f.adapter.build(authority, request);
    expect(f.producer.build).toHaveBeenCalledExactlyOnceWith(authority, {
      jobId: request.jobId, sourceCommit: request.sourceCommit, sourceTree: request.sourceTree,
    });
    expect(f.loadCandidate).toHaveBeenCalledTimes(2);
    expect(result).toEqual({ schema: "leaf.browser-candidate-preview.v1", candidate: witness, preview: f.preview });
    expect(Object.isFrozen(result.candidate)).toBe(true);
    expect(Object.isFrozen(result.preview.manifest.files[0])).toBe(true);
    expect(result.preview.manifest).not.toBe(f.preview.manifest);
  });

  for (const [field, value] of Object.entries({ sourceRequestId: id(55), version: 3, receiptDigest: "0".repeat(64),
    sourceCommit: "0".repeat(40), sourceTree: "0".repeat(40) })) {
    it(`rejects stale ${field} before any build`, async () => {
      const f = fixture();
      await expect(f.adapter.build(authority, { ...request, [field]: value })).rejects.toThrow("candidate_conflict");
      expect(f.producer.build).not.toHaveBeenCalled();
    });
  }
  for (const field of ["tenantId", "organizationId", "projectId", "repoKey"] as const) {
    it(`rejects foreign ${field} from the candidate resolver`, async () => {
      const f = fixture();
      f.loadCandidate.mockResolvedValue({ ...f.row, authority: { ...authority, [field]: id(99) } });
      await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
      expect(f.producer.build).not.toHaveBeenCalled();
    });
  }
  it("rejects unavailable, unpublished and non-Browser candidates", async () => {
    for (const row of [null, { ...fixture().row, state: "staged" }, { ...fixture().row, schema: "leaf.native-project-source.v1" }]) {
      const f = fixture();
      f.loadCandidate.mockResolvedValue(row as PublishedBrowserCandidate | null);
      await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
      expect(f.producer.build).not.toHaveBeenCalled();
    }
  });
  it.each([0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])("rejects malformed version %s", async version => {
    const f = fixture();
    await expect(f.adapter.build(authority, { ...request, version })).rejects.toThrow("candidate_conflict");
    expect(f.loadCandidate).not.toHaveBeenCalled();
  });
  it("deduplicates concurrent and later retries after rechecking access", async () => {
    const f = fixture();
    const [one, two] = await Promise.all([f.adapter.build(authority, request), f.adapter.build(authority, request)]);
    expect(one).toBe(two);
    expect(await f.adapter.build(authority, request)).toBe(one);
    expect(f.producer.build).toHaveBeenCalledOnce();
    f.loadCandidate.mockResolvedValue(null);
    await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
  });
  it("does not relabel a job as a different candidate at the same commit/tree", async () => {
    const f = fixture();
    await f.adapter.build(authority, request);
    const next = { ...request, sourceRequestId: id(10), version: 1, receiptDigest: "0".repeat(64) };
    f.loadCandidate.mockResolvedValue({ ...f.row, ...next });
    await expect(f.adapter.build(authority, next)).rejects.toThrow("job_conflict");
    expect(f.producer.build).toHaveBeenCalledOnce();
  });
  it("rejects candidate revocation or drift during a build", async () => {
    const f = fixture();
    f.loadCandidate.mockResolvedValueOnce(f.row).mockResolvedValue(null);
    await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
  });
  it("does not resubmit failed or uncertain producer work", async () => {
    const f = fixture();
    f.producer.build.mockRejectedValue(new Error("isolated_next_binding_unavailable"));
    await expect(f.adapter.build(authority, request)).rejects.toThrow("isolated_next_binding_unavailable");
    await expect(f.adapter.build(authority, request)).rejects.toThrow("isolated_next_binding_unavailable");
    expect(f.producer.build).toHaveBeenCalledOnce();
  });
  it.each(["pending", "required"] as const)("does not project unconfirmed cleanup %s", async cleanup => {
    const f = fixture();
    f.producer.status.mockReturnValue({ ready: true, cleanup });
    await expect(f.adapter.build(authority, request)).rejects.toThrow("preview_unavailable");
  });
  for (const field of ["jobId", "sourceCommit", "sourceTree", "manifestSha256"] as const) {
    it(`rejects misbound producer ${field}`, async () => {
      const f = fixture();
      f.producer.build.mockResolvedValue({ ...f.preview, [field]: "0".repeat(field === "jobId" ? 36 : field === "manifestSha256" ? 64 : 40) });
      await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
    });
  }
  it("rejects a manifest from another project even with a self-consistent digest", async () => {
    const f = fixture();
    const manifest = { ...f.preview.manifest, requestDigest: "0".repeat(64) };
    f.producer.build.mockResolvedValue({ ...f.preview, manifest, manifestSha256: staticManifestDigest(manifest) });
    await expect(f.adapter.build(authority, request)).rejects.toThrow("candidate_conflict");
  });
  it("copies caller and producer witnesses across asynchronous boundaries", async () => {
    const f = fixture();
    const mutable = { ...request };
    const build = f.adapter.build(authority, mutable);
    mutable.version = 99;
    const result = await build;
    (f.preview.manifest.files[0] as { path: string }).path = "/changed.html";
    expect(result.candidate.version).toBe(witness.version);
    expect(result.preview.manifest.files[0].path).toBe("/index.html");
  });
});
