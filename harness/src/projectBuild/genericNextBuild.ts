/** Studio Browser static-export contract. Actual managed adapters are supplied
 * by the protected composition root. This library does not adopt a provider.
 */
import { createHash } from "node:crypto";
import type { ProjectRepositoryAuthority, TenantRepoProvider } from "../ports/index.js";

export interface NextBuildRequest { jobId: string; sourceCommit: string; sourceTree: string }
export type NextBuildScope = Readonly<ProjectRepositoryAuthority & NextBuildRequest>;
export interface StaticFile { readonly path: string; readonly body: Buffer; readonly contentType: string }
export interface ManifestFile {
  readonly path: string; readonly sha256: string; readonly sizeBytes: number; readonly contentType: string;
}
export interface StaticManifest {
  readonly requestDigest: string; readonly bundleSha256: string;
  readonly bindingId: string; readonly toolchainSha256: string;
  readonly files: readonly ManifestFile[];
}
export interface NextPreview extends NextBuildRequest {
  readonly bundleSha256: string; readonly bindingId: string; readonly toolchainSha256: string;
  readonly artifactBindingId: string; readonly artifactId: string; readonly manifestSha256: string;
  readonly manifest: StaticManifest;
}

export const NEXT_PLAN = Object.freeze({
  build: Object.freeze(["/opt/leaf/node/bin/node", "/opt/leaf/next/dist/bin/next", "build"]),
  output: "out" as const,
  env: Object.freeze({ NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1" }),
  network: "deny" as const,
  timeoutMs: 120_000,
});

/** A real adapter must enforce safe source materialization, immutable toolchain
 * supply, filesystem/network isolation and resource limits inside its jail.
 * destroy confirms provider-observed cleanup. A void response is insufficient.
 */
export interface ManagedNextRuntime {
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly isolation: "filesystem-and-network";
  create(scope: NextBuildScope): Promise<{
    build(source: Buffer, bundleSha256: string, plan: typeof NEXT_PLAN): Promise<{
      requestDigest: string; bundleSha256: string; files: readonly StaticFile[];
    }>;
    destroy(): Promise<{ confirmed: boolean }>;
  }>;
}

/** The protected caller supplies an actual private immutable artifact producer.
 * put must persist complete bytes before acknowledging the manifest digest.
 * Both operations enforce the supplied scope and recheck current access.
 */
export interface PrivateStaticArtifacts {
  readonly bindingId: string;
  readonly access: "private-authenticated";
  readonly immutable: true;
  put(scope: NextBuildScope, manifest: StaticManifest, files: readonly StaticFile[]): Promise<{
    artifactId: string; manifestSha256: string;
  }>;
  read(scope: NextBuildScope, artifactId: string, path: string, maxBytes: number): Promise<{
    body: Buffer; contentType: string;
  }>;
}

export class NextBuildError extends Error {
  constructor(readonly code: string) { super(code); }
}
type Lease = Awaited<ReturnType<ManagedNextRuntime["create"]>>;
type Job = {
  scope: NextBuildScope; digest: string; promise: Promise<NextPreview>;
  lease?: Lease; preview?: NextPreview; released: boolean;
  cleanupPromise?: Promise<void>;
  cleanup: "not-created" | "required" | "pending" | "confirmed";
};
const uuid = (x: unknown): x is string => typeof x === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(x);
const sha = (x: unknown, n: number): x is string => typeof x === "string" &&
  x.length === n && /^[0-9a-f]+$/.test(x);
const hash = (x: Buffer | string) => createHash("sha256").update(x).digest("hex");
const projectKey = (a: ProjectRepositoryAuthority) => JSON.stringify([
  a.tenantId, a.organizationId, a.projectId, a.repoKey,
]);
const safePath = (path: unknown): path is string => typeof path === "string" &&
  path.length <= 2048 && /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(path) &&
  !path.split("/").some(part => part === "." || part === "..");
const contentType = (value: unknown): value is string => typeof value === "string" &&
  /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(value);
export const nextRequestDigest = (s: NextBuildScope) => hash(JSON.stringify([
  s.tenantId, s.organizationId, s.projectId, s.repoKey, s.jobId, s.sourceCommit, s.sourceTree,
]));
export const staticManifestDigest = (m: StaticManifest) => hash(JSON.stringify([
  m.requestDigest, m.bundleSha256, m.bindingId, m.toolchainSha256,
  m.files.map(f => [f.path, f.sha256, f.sizeBytes, f.contentType]),
]));

/** Jobs and last-valid references remain process-local. Durable reconciliation,
 * admission and private HTTP serving belong to the protected caller.
 */
export class GenericNextProducer {
  private readonly jobs = new Map<string, Job>();
  private readonly latest = new Map<string, NextPreview>();
  constructor(private readonly deps: {
    repository: Pick<TenantRepoProvider, "exportProjectSourceBundle">;
    runtime?: ManagedNextRuntime;
    artifacts?: PrivateStaticArtifacts;
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
        runtime.isolation !== "filesystem-and-network" || typeof runtime.create !== "function")
      throw new NextBuildError("isolated_next_binding_unavailable");
    const artifacts = this.deps.artifacts;
    if (!artifacts || !uuid(artifacts.bindingId) || artifacts.access !== "private-authenticated" ||
        artifacts.immutable !== true || typeof artifacts.put !== "function" || typeof artifacts.read !== "function")
      throw new NextBuildError("private_artifact_binding_unavailable");
    const job: Job = { scope, digest, released: false, cleanup: "not-created", promise: undefined! };
    this.jobs.set(key, job);
    job.promise = this.execute(job, runtime, artifacts);
    return job.promise;
  }

  private async execute(job: Job, runtime: ManagedNextRuntime, artifacts: PrivateStaticArtifacts): Promise<NextPreview> {
    try {
      await this.current(job.scope);
      const exporter = this.deps.repository.exportProjectSourceBundle;
      if (!exporter) throw new NextBuildError("source_unavailable");
      const source = await exporter.call(this.deps.repository, {
        tenantId: job.scope.tenantId, organizationId: job.scope.organizationId,
        projectId: job.scope.projectId, repoKey: job.scope.repoKey,
      }, { sourceCommit: job.scope.sourceCommit, sourceTree: job.scope.sourceTree });
      if (source.sourceCommit !== job.scope.sourceCommit || source.sourceTree !== job.scope.sourceTree ||
          !Buffer.isBuffer(source.bundle) || source.bundle.length < 1 || source.bundle.length > 64 * 1024 * 1024 ||
          source.sizeBytes !== source.bundle.length || !sha(source.bundleSha256, 64) ||
          hash(source.bundle) !== source.bundleSha256 || !uuid(source.leaseId) ||
          !/^[1-9][0-9]*$/.test(source.leaseGeneration)) throw new NextBuildError("source_conflict");
      await this.current(job.scope);
      job.lease = await runtime.create(job.scope);
      job.cleanup = "required";
      const result = await job.lease.build(Buffer.from(source.bundle), source.bundleSha256, NEXT_PLAN);
      if (result.requestDigest !== job.digest || result.bundleSha256 !== source.bundleSha256)
        throw new NextBuildError("producer_conflict");
      const files = this.exportedFiles(result.files);
      const manifest: StaticManifest = Object.freeze({
        requestDigest: job.digest, bundleSha256: source.bundleSha256,
        bindingId: runtime.bindingId, toolchainSha256: runtime.toolchainSha256,
        files: Object.freeze(files.map(f => Object.freeze({
          path: f.path, sha256: hash(f.body), sizeBytes: f.body.length, contentType: f.contentType,
        }))),
      });
      const manifestSha256 = staticManifestDigest(manifest);
      await this.current(job.scope);
      const stored = await artifacts.put(job.scope, manifest, files);
      if (!uuid(stored.artifactId) || stored.manifestSha256 !== manifestSha256)
        throw new NextBuildError("artifact_conflict");
      await this.cleanup(job);
      await this.current(job.scope);
      const preview: NextPreview = Object.freeze({
        jobId: job.scope.jobId, sourceCommit: job.scope.sourceCommit, sourceTree: job.scope.sourceTree,
        bundleSha256: source.bundleSha256, bindingId: runtime.bindingId,
        toolchainSha256: runtime.toolchainSha256, artifactBindingId: artifacts.bindingId,
        artifactId: stored.artifactId, manifestSha256, manifest,
      });
      job.preview = preview;
      this.latest.set(projectKey(job.scope), preview);
      return preview;
    } catch (error) {
      if (job.lease && job.cleanup === "required") await this.cleanup(job);
      throw error instanceof NextBuildError ? error : new NextBuildError("build_failed");
    }
  }

  private exportedFiles(value: readonly StaticFile[]): StaticFile[] {
    if (!Array.isArray(value) || value.length < 1 || value.length > 4096)
      throw new NextBuildError("artifact_conflict");
    const paths = new Set<string>();
    let total = 0;
    const files = value.map(f => {
      if (!f || !safePath(f.path) || paths.has(f.path) || !Buffer.isBuffer(f.body) ||
          f.body.length > 2 * 1024 * 1024 || !contentType(f.contentType))
        throw new NextBuildError("artifact_conflict");
      paths.add(f.path);
      total += f.body.length;
      if (total > 32 * 1024 * 1024) throw new NextBuildError("artifact_conflict");
      return Object.freeze({ path: f.path, body: Buffer.from(f.body), contentType: f.contentType });
    });
    if (!paths.has("/index.html")) throw new NextBuildError("artifact_conflict");
    return files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  }

  private cleanup(job: Job): Promise<void> {
    if (job.cleanupPromise) return job.cleanupPromise;
    job.cleanupPromise = this.confirmCleanup(job).finally(() => { job.cleanupPromise = undefined; });
    return job.cleanupPromise;
  }

  private async confirmCleanup(job: Job): Promise<void> {
    job.cleanup = "pending";
    try {
      const result = await job.lease!.destroy();
      if (result?.confirmed !== true) throw new Error("unconfirmed");
    } catch { throw new NextBuildError("cleanup_required"); }
    job.lease = undefined;
    job.cleanup = "confirmed";
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
    const preview = job.preview;
    if (!preview || job.cleanup !== "confirmed") throw new NextBuildError("preview_unavailable");
    const assetPath = path === "/" ? "/index.html" : path;
    if (!safePath(assetPath)) throw new NextBuildError("preview_path_invalid");
    const file = preview.manifest.files.find(f => f.path === assetPath);
    if (!file) throw new NextBuildError("preview_unavailable");
    await this.current(job.scope);
    let response: Awaited<ReturnType<PrivateStaticArtifacts["read"]>>;
    try { response = await this.deps.artifacts!.read(job.scope, preview.artifactId, assetPath, file.sizeBytes); }
    catch { throw new NextBuildError("preview_unavailable"); }
    await this.current(job.scope);
    if (job.released) throw new NextBuildError("preview_unavailable");
    if (!Buffer.isBuffer(response.body) || response.body.length !== file.sizeBytes ||
        hash(response.body) !== file.sha256 || response.contentType !== file.contentType)
      throw new NextBuildError("preview_response_invalid");
    return { body: Buffer.from(response.body), contentType: response.contentType };
  }

  status(authority: ProjectRepositoryAuthority, jobId: string) {
    const job = this.owned(authority, jobId);
    return Object.freeze({ cleanup: job.cleanup, ready: !!job.preview && job.cleanup === "confirmed" });
  }
  lastValid(authority: ProjectRepositoryAuthority): NextPreview | undefined {
    this.validateAuthority(authority);
    return this.latest.get(projectKey(authority));
  }
  async release(authority: ProjectRepositoryAuthority, jobId: string): Promise<void> {
    const job = this.owned(authority, jobId);
    await job.promise.catch(() => undefined);
    if (job.lease) await this.cleanup(job);
    job.released = true;
    if (this.latest.get(projectKey(authority))?.jobId === jobId) this.latest.delete(projectKey(authority));
  }
}
