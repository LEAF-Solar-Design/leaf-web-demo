/** Studio Browser producer. This library is not a runtime binding or public route.
 * Only the server composition root may supply authority and a managed runtime.
 * Never connect LocalProcessSubstrate or caller-provided commands/preview URLs.
 */
import { createHash } from "node:crypto";
import type { ProjectRepositoryAuthority, TenantRepoProvider } from "../ports/index.js";

export interface NextBuildRequest { jobId: string; sourceCommit: string; sourceTree: string }
export type NextBuildScope = Readonly<ProjectRepositoryAuthority & NextBuildRequest>;
export interface NextPreview {
  readonly jobId: string;
  readonly sourceCommit: string;
  readonly sourceTree: string;
  readonly bundleSha256: string;
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly previewId: string;
}

// The managed binding must enforce this plan inside its existing jail. There
// is deliberately no shell/package-script/install command or inherited env.
export const NEXT_PLAN = Object.freeze({
  build: Object.freeze(["/opt/leaf/node/bin/node", "/opt/leaf/next/dist/bin/next", "build"]),
  serve: Object.freeze(["/opt/leaf/node/bin/node", "/opt/leaf/next/dist/bin/next", "start", "--hostname", "0.0.0.0", "--port", "3000"]),
  env: Object.freeze({ NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1" }),
  network: "deny" as const,
  timeoutMs: 120_000,
  previewTtlMs: 300_000,
});

/** Adapter for an EXISTING operator-managed resource, not a claim that one is
 * installed. It must enforce filesystem/network isolation, safe Git-bundle
 * materialization (no traversal/symlink escape), immutable dependency supply,
 * hard timeouts/TTL and authenticated private ingress. No workspace credentials.
 */
export interface ManagedNextRuntime {
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly isolation: "filesystem-and-network";
  readonly ingress: "private-authenticated";
  create(scope: NextBuildScope): Promise<{
    build(source: Buffer, bundleSha256: string, plan: typeof NEXT_PLAN): Promise<{
      requestDigest: string; previewId: string; bundleSha256: string;
    }>;
    read(previewId: string, path: string, maxBytes: number): Promise<{
      body: Buffer; contentType: string;
    }>;
    destroy(): Promise<void>;
  }>;
}

export class NextBuildError extends Error {
  constructor(readonly code: string) { super(code); }
}
type Lease = Awaited<ReturnType<ManagedNextRuntime["create"]>>;
type Job = {
  scope: NextBuildScope; digest: string; promise: Promise<NextPreview>;
  lease?: Lease; preview?: NextPreview; released: boolean;
};
const uuid = (x: unknown): x is string => typeof x === "string" && x.length === 36 &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(x);
const sha = (x: unknown, n: number): x is string => typeof x === "string" &&
  x.length === n && /^[0-9a-f]+$/.test(x);
const hash = (x: Buffer | string) => createHash("sha256").update(x).digest("hex");
const projectKey = (a: ProjectRepositoryAuthority) => JSON.stringify([
  a.tenantId, a.organizationId, a.projectId, a.repoKey,
]);
export const nextRequestDigest = (s: NextBuildScope) => hash(JSON.stringify([
  s.tenantId, s.organizationId, s.projectId, s.repoKey, s.jobId, s.sourceCommit, s.sourceTree,
]));

/** Jobs/last-valid records are process-local. A future protected caller must
 * persist receipts/reconcile runtime leases for reopen; this is not that route.
 */
export class GenericNextProducer {
  private readonly jobs = new Map<string, Job>();
  private readonly latest = new Map<string, NextPreview>();
  constructor(private readonly deps: {
    repository: Pick<TenantRepoProvider, "exportProjectSourceBundle">;
    runtime?: ManagedNextRuntime;
    isCurrent(scope: NextBuildScope): Promise<boolean>;
  }) {}

  build(authority: ProjectRepositoryAuthority, request: NextBuildRequest): Promise<NextPreview> {
    this.validateAuthority(authority);
    if (!uuid(request.jobId) || !sha(request.sourceCommit, 40) || !sha(request.sourceTree, 40))
      throw new NextBuildError("source_conflict");
    const scope = Object.freeze({
      tenantId: authority.tenantId, organizationId: authority.organizationId,
      projectId: authority.projectId, repoKey: authority.repoKey,
      jobId: request.jobId, sourceCommit: request.sourceCommit, sourceTree: request.sourceTree,
    });
    const key = projectKey(scope) + scope.jobId;
    const digest = nextRequestDigest(scope);
    const prior = this.jobs.get(key);
    if (prior) {
      if (prior.digest !== digest) throw new NextBuildError("job_conflict");
      if (prior.released) throw new NextBuildError("preview_released");
      return prior.promise;
    }
    const runtime = this.deps.runtime;
    if (!runtime || !uuid(runtime.bindingId) || !sha(runtime.toolchainSha256, 64) ||
        runtime.isolation !== "filesystem-and-network" || runtime.ingress !== "private-authenticated")
      throw new NextBuildError("isolated_next_binding_unavailable");
    const job: Job = { scope, digest, released: false, promise: undefined! };
    // Store before any async provider call so identical submissions join once.
    this.jobs.set(key, job);
    job.promise = this.execute(job, runtime);
    return job.promise;
  }

  private async execute(job: Job, runtime: ManagedNextRuntime): Promise<NextPreview> {
    try {
      await this.current(job.scope);
      const exporter = this.deps.repository.exportProjectSourceBundle;
      if (!exporter) throw new NextBuildError("source_unavailable");
      const source = await exporter.call(this.deps.repository, {
        tenantId: job.scope.tenantId, organizationId: job.scope.organizationId,
        projectId: job.scope.projectId, repoKey: job.scope.repoKey,
      }, {
        sourceCommit: job.scope.sourceCommit, sourceTree: job.scope.sourceTree,
      });
      if (source.sourceCommit !== job.scope.sourceCommit || source.sourceTree !== job.scope.sourceTree ||
          !Buffer.isBuffer(source.bundle) || source.bundle.length < 1 || source.bundle.length > 64 * 1024 * 1024 ||
          source.sizeBytes !== source.bundle.length || !sha(source.bundleSha256, 64) ||
          hash(source.bundle) !== source.bundleSha256 || !uuid(source.leaseId) ||
          !/^[1-9][0-9]*$/.test(source.leaseGeneration)) throw new NextBuildError("source_conflict");
      await this.current(job.scope);
      job.lease = await runtime.create(job.scope);
      const result = await job.lease.build(Buffer.from(source.bundle), source.bundleSha256, NEXT_PLAN);
      if (result.requestDigest !== job.digest || result.bundleSha256 !== source.bundleSha256 ||
          !uuid(result.previewId)) throw new NextBuildError("producer_conflict");
      await this.current(job.scope);
      const preview = Object.freeze({ jobId: job.scope.jobId, sourceCommit: job.scope.sourceCommit,
        sourceTree: job.scope.sourceTree, bundleSha256: source.bundleSha256,
        bindingId: runtime.bindingId, toolchainSha256: runtime.toolchainSha256,
        previewId: result.previewId });
      job.preview = preview;
      this.latest.set(projectKey(job.scope), preview);
      return preview;
    } catch (error) {
      if (job.lease) {
        try { await job.lease.destroy(); job.lease = undefined; }
        catch { throw new NextBuildError("cleanup_required"); }
      }
      throw error instanceof NextBuildError ? error : new NextBuildError("build_failed");
    }
  }

  private validateAuthority(a: ProjectRepositoryAuthority): void {
    if (!a || ![a.tenantId, a.organizationId, a.projectId, a.repoKey].every(uuid))
      throw new NextBuildError("authority_conflict");
  }
  private async current(scope: NextBuildScope): Promise<void> {
    let current: boolean;
    try { current = await this.deps.isCurrent(scope); }
    catch { throw new NextBuildError("source_unavailable"); }
    if (!current) throw new NextBuildError("source_stale");
  }
  private owned(a: ProjectRepositoryAuthority, jobId: string): Job {
    this.validateAuthority(a);
    if (!uuid(jobId)) throw new NextBuildError("preview_unavailable");
    const job = this.jobs.get(projectKey(a) + jobId);
    if (!job || job.released) throw new NextBuildError("preview_unavailable");
    return job;
  }

  async read(authority: ProjectRepositoryAuthority, jobId: string, path = "/") {
    const job = this.owned(authority, jobId);
    if (!job.preview || !job.lease) throw new NextBuildError("preview_unavailable");
    if (typeof path !== "string" || path.length > 2048 || !path.startsWith("/") ||
        path.startsWith("//") || /[\\\x00-\x20#%]/.test(path) ||
        path.split(/[/?]/).some(part => part === "..")) throw new NextBuildError("preview_path_invalid");
    await this.current(job.scope);
    let response: Awaited<ReturnType<Lease["read"]>>;
    try { response = await job.lease.read(job.preview.previewId, path, 2 * 1024 * 1024); }
    catch { throw new NextBuildError("preview_unavailable"); }
    // Refuse late data after a project/source switch, even if provider succeeded.
    await this.current(job.scope);
    if (!Buffer.isBuffer(response.body) || response.body.length > 2 * 1024 * 1024 ||
        !/^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(response.contentType))
      throw new NextBuildError("preview_response_invalid");
    return { body: Buffer.from(response.body), contentType: response.contentType };
  }

  lastValid(authority: ProjectRepositoryAuthority): NextPreview | undefined {
    this.validateAuthority(authority);
    return this.latest.get(projectKey(authority));
  }

  async release(authority: ProjectRepositoryAuthority, jobId: string): Promise<void> {
    const job = this.owned(authority, jobId);
    // Do not race destroy against an in-flight source/build operation.
    await job.promise.catch(() => undefined);
    if (job.lease) {
      try { await job.lease.destroy(); job.lease = undefined; }
      catch { throw new NextBuildError("cleanup_required"); }
    }
    job.released = true;
    if (this.latest.get(projectKey(authority))?.jobId === jobId)
      this.latest.delete(projectKey(authority));
  }
}
