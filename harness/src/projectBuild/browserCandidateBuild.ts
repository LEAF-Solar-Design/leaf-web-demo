/** B01 candidate identity adapter. No runtime, HTTP route or provider adoption.
 * The protected caller loads candidates and resolves authority under its current
 * authenticated project scope. Jobs remain process-local, like the producer.
 */
import type { ProjectRepositoryAuthority } from "../ports/index.js";
import { NextBuildError, nextRequestDigest, staticManifestDigest } from "./genericNextBuild.js";
import type { GenericNextProducer, NextPreview } from "./genericNextBuild.js";

export interface BrowserCandidateWitness {
  readonly sourceRequestId: string;
  readonly version: number;
  readonly receiptDigest: string;
  readonly sourceCommit: string;
  readonly sourceTree: string;
}
/** Normalized server readback, never authority accepted from a browser body. */
export interface PublishedBrowserCandidate extends BrowserCandidateWitness {
  readonly schema: "leaf.browser-project-source.v1";
  readonly state: "published";
  readonly authority: ProjectRepositoryAuthority;
}
export interface BrowserCandidateBuildRequest extends BrowserCandidateWitness { readonly jobId: string }
export interface BrowserCandidatePreview {
  readonly schema: "leaf.browser-candidate-preview.v1";
  readonly candidate: BrowserCandidateWitness;
  readonly preview: NextPreview;
}
const uuid = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, n: number): v is string => typeof v === "string" && new RegExp(`^[0-9a-f]{${n}}$`).test(v);
const conflict = (): never => { throw new NextBuildError("candidate_conflict"); };
const publicCodes = new Set([
  "candidate_conflict", "candidate_unavailable", "job_conflict", "authority_conflict",
  "isolated_next_binding_unavailable", "private_artifact_binding_unavailable", "source_conflict",
  "source_unavailable", "source_stale", "producer_conflict", "artifact_conflict", "build_failed",
  "cleanup_required", "preview_unavailable", "preview_released", "preview_path_invalid", "preview_response_invalid",
]);
/** Port failures may contain provider diagnostics. Never propagate their object,
 * message, cause or extra properties into a caller-visible adapter failure.
 */
function publicFailure(error: unknown): NextBuildError {
  let code = "build_failed";
  try { if (error instanceof NextBuildError && publicCodes.has(error.code)) code = error.code; } catch { /* Untrusted accessor. */ }
  return new NextBuildError(code);
}
function authorityCopy(a: ProjectRepositoryAuthority): ProjectRepositoryAuthority {
  if (!a || ![a.tenantId, a.organizationId, a.projectId, a.repoKey].every(uuid)) conflict();
  return Object.freeze({ tenantId: a.tenantId, organizationId: a.organizationId, projectId: a.projectId, repoKey: a.repoKey });
}
const authorityKey = (a: ProjectRepositoryAuthority) => JSON.stringify([a.tenantId, a.organizationId, a.projectId, a.repoKey]);
function witnessCopy(w: BrowserCandidateWitness): BrowserCandidateWitness {
  if (!w || !uuid(w.sourceRequestId) || !Number.isSafeInteger(w.version) || w.version < 1 ||
      !sha(w.receiptDigest, 64) || !sha(w.sourceCommit, 40) || !sha(w.sourceTree, 40)) conflict();
  return Object.freeze({ sourceRequestId: w.sourceRequestId, version: w.version, receiptDigest: w.receiptDigest,
    sourceCommit: w.sourceCommit, sourceTree: w.sourceTree });
}
const witnessKey = (w: BrowserCandidateWitness) => JSON.stringify([
  w.sourceRequestId, w.version, w.receiptDigest, w.sourceCommit, w.sourceTree,
]);

/** Reuse B03's producer; retain the extra source-candidate witnesses it does not
 * take. A repeated job cannot relabel even the same commit/tree as another
 * candidate. Unknown/failed jobs are not resubmitted by this adapter.
 */
export class BrowserCandidateBuildAdapter {
  private readonly jobs = new Map<string, { witness: string; result: Promise<BrowserCandidatePreview> }>();
  constructor(private readonly deps: {
    producer: Pick<GenericNextProducer, "build" | "status">;
    loadCandidate(authority: ProjectRepositoryAuthority, sourceRequestId: string): Promise<PublishedBrowserCandidate | null>;
  }) {}

  private async current(a: ProjectRepositoryAuthority, w: BrowserCandidateWitness): Promise<void> {
    let row: PublishedBrowserCandidate | null;
    try { row = await this.deps.loadCandidate(a, w.sourceRequestId); }
    catch { throw new NextBuildError("candidate_unavailable"); }
    if (!row || row.schema !== "leaf.browser-project-source.v1" || row.state !== "published" ||
        authorityKey(authorityCopy(row.authority)) !== authorityKey(a) || witnessKey(witnessCopy(row)) !== witnessKey(w)) conflict();
  }

  async build(authority: ProjectRepositoryAuthority, request: BrowserCandidateBuildRequest): Promise<BrowserCandidatePreview> {
    const a = authorityCopy(authority);
    const w = witnessCopy(request);
    const jobId = request.jobId;
    if (!uuid(jobId)) conflict();
    const key = authorityKey(a) + jobId;
    const identity = witnessKey(w);
    await this.current(a, w);
    const prior = this.jobs.get(key);
    if (prior) {
      if (prior.witness !== identity) throw new NextBuildError("job_conflict");
      return prior.result;
    }
    const result = Promise.resolve().then(() => this.execute(a, w, jobId))
      .catch(error => { throw publicFailure(error); });
    this.jobs.set(key, { witness: identity, result });
    return result;
  }

  private async execute(a: ProjectRepositoryAuthority, w: BrowserCandidateWitness, jobId: string): Promise<BrowserCandidatePreview> {
    const request = Object.freeze({ jobId, sourceCommit: w.sourceCommit, sourceTree: w.sourceTree });
    const value = await this.deps.producer.build(a, request);
    // Copy before another await so a mutable producer response cannot relabel
    // the terminal projection. Asset bytes remain behind the producer's read.
    const preview = this.previewCopy(a, request, value);
    await this.current(a, w);
    const status = this.deps.producer.status(a, jobId);
    if (!status || Object.keys(status).sort().join(",") !== "cleanup,ready" ||
        status.ready !== true || status.cleanup !== "confirmed") throw new NextBuildError("preview_unavailable");
    return Object.freeze({ schema: "leaf.browser-candidate-preview.v1", candidate: w, preview });
  }

  private previewCopy(a: ProjectRepositoryAuthority, request: { jobId: string; sourceCommit: string; sourceTree: string }, p: NextPreview): NextPreview {
    try {
      if (!p || p.jobId !== request.jobId || p.sourceCommit !== request.sourceCommit || p.sourceTree !== request.sourceTree ||
          !sha(p.bundleSha256, 64) || !uuid(p.bindingId) || !sha(p.toolchainSha256, 64) ||
          !uuid(p.artifactBindingId) || !uuid(p.artifactId) || !sha(p.manifestSha256, 64) ||
          p.manifest.requestDigest !== nextRequestDigest({ ...a, ...request }) ||
          p.manifest.bundleSha256 !== p.bundleSha256 || p.manifest.bindingId !== p.bindingId ||
          p.manifest.toolchainSha256 !== p.toolchainSha256 || staticManifestDigest(p.manifest) !== p.manifestSha256) conflict();
      const manifest = Object.freeze({ ...p.manifest, files: Object.freeze(p.manifest.files.map(f => Object.freeze({ ...f }))) });
      return Object.freeze({ ...request, bundleSha256: p.bundleSha256, bindingId: p.bindingId,
        toolchainSha256: p.toolchainSha256, artifactBindingId: p.artifactBindingId, artifactId: p.artifactId,
        manifestSha256: p.manifestSha256, manifest });
    } catch { return conflict(); }
  }
}
