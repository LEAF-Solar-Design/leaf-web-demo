/** Static Next job adapter. The injected executor is a trusted managed transport,
 * never tenant code or a desktop process. This module does not provision it.
 */
import { createHash } from "node:crypto";
import { NEXT_PLAN, NextBuildError, nextRequestDigest } from "./genericNextBuild.js";
import type { ManagedNextRuntime, NextBuildScope, StaticFile } from "./genericNextBuild.js";

export interface StaticNextJobIdentity {
  readonly protocol: "leaf.static-next.v1";
  readonly executionId: string;
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly scope: NextBuildScope;
}
export interface StaticNextJobInput extends StaticNextJobIdentity {
  readonly bundle: Buffer;
  readonly bundleSha256: string;
  readonly plan: typeof NEXT_PLAN;
}
export interface StaticNextJobResult {
  readonly protocol: "leaf.static-next.v1";
  readonly executionId: string;
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly bundleSha256: string;
  /** 0 exported, 10 build failure, 20 timeout, 30 policy, 40 source, 50 supply. */
  readonly exitCode: number;
  readonly files: readonly StaticFile[];
}
export interface StaticNextStopResult {
  readonly protocol: "leaf.static-next.v1";
  readonly executionId: string;
  readonly bindingId: string;
  readonly confirmed: boolean;
}
/** run is idempotent on executionId, rejecting different input. stop fences any
 * queued/running admission for that identity and confirms provider termination,
 * including an uncertain run submission. No credentials enter bundle or plan.
 */
export interface StaticNextJobExecutor {
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly isolation: "filesystem-and-network";
  run(input: StaticNextJobInput): Promise<StaticNextJobResult>;
  stop(identity: StaticNextJobIdentity): Promise<StaticNextStopResult>;
}

const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, size = 64): v is string => typeof v === "string" &&
  new RegExp(`^[0-9a-f]{${size}}$`).test(v);
const hash = (v: Buffer) => createHash("sha256").update(v).digest("hex");
const fail = (code: string): never => { throw new NextBuildError(code); };
function copyScope(s: NextBuildScope): NextBuildScope {
  if (!s || ![s.tenantId, s.organizationId, s.projectId, s.repoKey, s.jobId].every(uuid) ||
      !sha(s.sourceCommit, 40) || !sha(s.sourceTree, 40)) fail("source_conflict");
  return Object.freeze({ tenantId: s.tenantId, organizationId: s.organizationId,
    projectId: s.projectId, repoKey: s.repoKey, jobId: s.jobId,
    sourceCommit: s.sourceCommit, sourceTree: s.sourceTree });
}
function filesCopy(value: readonly StaticFile[]): readonly StaticFile[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > 4096) fail("producer_conflict");
  const paths = new Set<string>();
  let total = 0;
  const files = value.map(f => {
    if (!f || typeof f.path !== "string" || f.path.length > 2048 ||
        !/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(f.path) ||
        f.path.split("/").some(p => p === "." || p === "..") || paths.has(f.path) ||
        !Buffer.isBuffer(f.body) || f.body.length > 2 * 1024 * 1024 ||
        typeof f.contentType !== "string" || f.contentType.length > 256 ||
        !/^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(f.contentType)) fail("producer_conflict");
    total += f.body.length;
    if (total > 32 * 1024 * 1024) fail("producer_conflict");
    paths.add(f.path);
    return Object.freeze({ path: f.path, body: Buffer.from(f.body), contentType: f.contentType });
  });
  if (!paths.has("/index.html")) fail("producer_conflict");
  return Object.freeze(files.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

export class ExecutorManagedNextRuntime implements ManagedNextRuntime {
  readonly bindingId: string;
  readonly toolchainSha256: string;
  readonly isolation = "filesystem-and-network" as const;
  private readonly run: StaticNextJobExecutor["run"];
  private readonly stop: StaticNextJobExecutor["stop"];

  constructor(executor: StaticNextJobExecutor) {
    if (!executor || !uuid(executor.bindingId) || !sha(executor.toolchainSha256) ||
        executor.isolation !== this.isolation || typeof executor.run !== "function" ||
        typeof executor.stop !== "function") fail("isolated_next_binding_unavailable");
    this.bindingId = executor.bindingId;
    this.toolchainSha256 = executor.toolchainSha256;
    this.run = executor.run.bind(executor);
    this.stop = executor.stop.bind(executor);
  }

  async create(value: NextBuildScope): Promise<Awaited<ReturnType<ManagedNextRuntime["create"]>>> {
    const scope = copyScope(value);
    const identity: StaticNextJobIdentity = Object.freeze({ protocol: "leaf.static-next.v1",
      executionId: nextRequestDigest(scope), bindingId: this.bindingId,
      toolchainSha256: this.toolchainSha256, scope });
    let digest: string | undefined;
    let pending: Promise<{ requestDigest: string; bundleSha256: string; files: readonly StaticFile[] }> | undefined;
    let closing = false;
    let confirmed = false;
    let stopping: Promise<{ confirmed: boolean }> | undefined;
    return Object.freeze({
      build: (source: Buffer, bundleSha256: string, plan: typeof NEXT_PLAN) => {
        if (closing) return Promise.reject(new NextBuildError("preview_released"));
        // Only the exact immutable producer plan is accepted; callers cannot
        // insert commands, environment values, writable supply or egress.
        if (plan !== NEXT_PLAN || !Buffer.isBuffer(source) || source.length < 1 ||
            source.length > 64 * 1024 * 1024 || !sha(bundleSha256) || hash(source) !== bundleSha256)
          return Promise.reject(new NextBuildError("source_conflict"));
        if (digest && digest !== bundleSha256) return Promise.reject(new NextBuildError("job_conflict"));
        if (pending) return pending;
        digest = bundleSha256;
        const input: StaticNextJobInput = Object.freeze({ ...identity,
          bundle: Buffer.from(source), bundleSha256, plan: NEXT_PLAN });
        // One submission even after an uncertain transport outcome. Cleanup
        // addresses the deterministic identity rather than a possibly lost ID.
        pending = (async () => {
          let result: StaticNextJobResult;
          try { result = await this.run(input); }
          catch { return fail("build_failed"); }
          if (closing) fail("preview_released");
          if (!result || result.protocol !== identity.protocol || result.executionId !== identity.executionId ||
              result.bindingId !== identity.bindingId || result.toolchainSha256 !== identity.toolchainSha256 ||
              result.bundleSha256 !== bundleSha256) fail("producer_conflict");
          if (result.exitCode !== 0) {
            const errors: Record<number, string> = { 10: "build_failed", 20: "build_timeout",
              30: "runtime_policy_failed", 40: "source_conflict", 50: "toolchain_unavailable" };
            fail(errors[result.exitCode] ?? "producer_conflict");
          }
          return Object.freeze({ requestDigest: identity.executionId, bundleSha256,
            files: filesCopy(result.files) });
        })();
        return pending;
      },
      destroy: () => {
        closing = true;
        if (confirmed) return Promise.resolve({ confirmed: true });
        if (stopping) return stopping;
        stopping = (async () => {
          try {
            const receipt = await this.stop(identity);
            if (!receipt || receipt.protocol !== identity.protocol || receipt.executionId !== identity.executionId ||
                receipt.bindingId !== identity.bindingId || receipt.confirmed !== true) fail("cleanup_required");
            confirmed = true;
            return { confirmed: true };
          } catch { return fail("cleanup_required"); }
        })().finally(() => { stopping = undefined; });
        return stopping;
      },
    });
  }
}
