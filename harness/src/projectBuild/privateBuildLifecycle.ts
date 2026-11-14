/** Protected Browser build/check service. Observations never start execution.
 * This bounded catalog is process-local. A restart returns unknown, never a
 * synthetic ready result. Durable admission/recovery remains host-owned.
 */
import type { ProjectRepositoryAuthority } from "../ports/index.js";
import { NextBuildError } from "./genericNextBuild.js";
import type { GenericNextProducer } from "./genericNextBuild.js";
import { BrowserCandidateBuildAdapter } from "./browserCandidateBuild.js";
import type { BrowserCandidateBuildRequest, BrowserCandidatePreview, PublishedBrowserCandidate } from "./browserCandidateBuild.js";

type Loader = (authority: ProjectRepositoryAuthority, requestId: string) => Promise<PublishedBrowserCandidate | null>;
type Record = { identity: string; result?: BrowserCandidatePreview; failed: boolean };
export type PrivateBuildObservation = Readonly<{
  schema: "leaf.private-static-build.v1";
  jobId: string;
  status: "unknown" | "building" | "failed" | "ready";
  result?: BrowserCandidatePreview;
}>;
const uuid = (v: unknown): v is string => typeof v === "string" && /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, n: number): v is string => typeof v === "string" && new RegExp(`^[0-9a-f]{${n}}$`).test(v);
const scopeKey = (a: ProjectRepositoryAuthority) => JSON.stringify([a.tenantId,a.organizationId,a.projectId,a.repoKey]);
const witnessKey = (r: BrowserCandidateBuildRequest) => JSON.stringify([r.sourceRequestId,r.version,r.receiptDigest,r.sourceCommit,r.sourceTree]);

export class PrivateBuildLifecycle {
  private readonly jobs = new Map<string, Record>();
  private readonly adapter: BrowserCandidateBuildAdapter;
  constructor(private readonly deps: {
    producer: Pick<GenericNextProducer,"build"|"status"|"lastValid">;
    loadCandidate: Loader;
  }) { this.adapter = new BrowserCandidateBuildAdapter(deps); }

  private snapshot(a: ProjectRepositoryAuthority, r: BrowserCandidateBuildRequest) {
    if (!a || !r || ![a.tenantId,a.organizationId,a.projectId,a.repoKey,r.jobId,r.sourceRequestId].every(uuid) ||
        !Number.isSafeInteger(r.version) || r.version < 1 || !sha(r.receiptDigest,64) ||
        !sha(r.sourceCommit,40) || !sha(r.sourceTree,40)) throw new NextBuildError("candidate_conflict");
    return { a:Object.freeze({tenantId:a.tenantId,organizationId:a.organizationId,projectId:a.projectId,repoKey:a.repoKey}),
      r:Object.freeze({jobId:r.jobId,sourceRequestId:r.sourceRequestId,version:r.version,receiptDigest:r.receiptDigest,sourceCommit:r.sourceCommit,sourceTree:r.sourceTree}) };
  }
  private async current(a: ProjectRepositoryAuthority, r: BrowserCandidateBuildRequest) {
    const row = await this.deps.loadCandidate(a,r.sourceRequestId);
    if (!row || row.schema !== "leaf.browser-project-source.v1" || row.state !== "published" ||
        !row.authority || scopeKey(row.authority) !== scopeKey(a) ||
        witnessKey({...row,jobId:r.jobId}) !== witnessKey(r)) throw new NextBuildError("candidate_conflict");
  }

  async submit(authority: ProjectRepositoryAuthority, request: BrowserCandidateBuildRequest): Promise<PrivateBuildObservation> {
    const {a,r} = this.snapshot(authority,request);
    await this.current(a,r);
    const key = scopeKey(a)+r.jobId;
    const prior = this.jobs.get(key);
    if (prior && prior.identity !== witnessKey(r)) throw new NextBuildError("job_conflict");
    if (!prior) {
      // Do not evict failed or uncertain identities and accidentally resubmit.
      if (this.jobs.size >= 1024) throw new NextBuildError("preview_capacity");
      const entry: Record = {identity:witnessKey(r),failed:false};
      this.jobs.set(key,entry);
      void this.adapter.build(a,r).then(result => { entry.result=result; }, () => { entry.failed=true; });
    }
    return this.observe(a,r);
  }

  async observe(authority: ProjectRepositoryAuthority, request: BrowserCandidateBuildRequest): Promise<PrivateBuildObservation> {
    const {a,r} = this.snapshot(authority,request);
    await this.current(a,r);
    const entry = this.jobs.get(scopeKey(a)+r.jobId);
    if (entry && entry.identity !== witnessKey(r)) throw new NextBuildError("job_conflict");
    let status: PrivateBuildObservation["status"] = !entry ? "unknown" : entry.failed ? "failed" : "building";
    let result: BrowserCandidatePreview | undefined;
    if (entry?.result) {
      const state = this.deps.producer.status(a,r.jobId);
      const latest = this.deps.producer.lastValid(a);
      const p = entry.result.preview;
      if (!state.ready || state.cleanup !== "confirmed" || !latest || latest.jobId !== r.jobId ||
          latest.sourceCommit !== r.sourceCommit || latest.sourceTree !== r.sourceTree ||
          latest.artifactId !== p.artifactId || latest.manifestSha256 !== p.manifestSha256)
        throw new NextBuildError("preview_unavailable");
      status="ready"; result=entry.result;
    }
    await this.current(a,r);
    if (result) {
      const state=this.deps.producer.status(a,r.jobId),latest=this.deps.producer.lastValid(a);
      if (!state.ready || state.cleanup !== "confirmed" || !latest || latest.jobId !== r.jobId ||
          latest.artifactId !== result.preview.artifactId || latest.manifestSha256 !== result.preview.manifestSha256)
        throw new NextBuildError("preview_unavailable");
    }
    return Object.freeze({schema:"leaf.private-static-build.v1",jobId:r.jobId,status,...(result?{result}:{})});
  }
}

/** One producer supplies execution, cleanup status and asset reads. */
export function createPrivateBuildServices(options: {producer: GenericNextProducer; loadCandidate: Loader}) {
  return {projectStaticPreviews:options.producer,projectStaticBuilds:new PrivateBuildLifecycle(options)};
}
