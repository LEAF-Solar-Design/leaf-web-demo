import { createHash } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { NEXT_PLAN, NextBuildError, nextRequestDigest } from "./genericNextBuild.js";
import type { NextBuildScope, StaticFile } from "./genericNextBuild.js";
import type { StaticNextJobExecutor, StaticNextJobIdentity, StaticNextJobInput, StaticNextJobResult } from "./managedNextRuntime.js";

export interface StaticNextObjectStore {
  /** Create only if absent; never overwrite an admitted input or mapping. */
  putIfAbsent(key: string, body: Buffer): Promise<"created" | "exists">;
  get(key: string): Promise<Buffer | null>;
}
export type StaticNextBuildState = "QUEUED" | "IN_PROGRESS" | "SUCCEEDED" | "FAILED" | "FAULT" | "TIMED_OUT" | "STOPPED";
export interface StaticNextBuildProvider {
  /** Start the exec job with EXECUTION_ID set. */
  start(executionId: string): Promise<string>;
  state(buildId: string): Promise<StaticNextBuildState>;
  stop(buildId: string): Promise<void>;
}
export interface CodeBuildStaticNextExecutorOptions {
  bindingId: string;
  toolchainSha256: string;
  store: StaticNextObjectStore;
  provider: StaticNextBuildProvider;
  pollMs?: number;
  deadlineMs?: number;
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

const protocol = "leaf.static-next.v1" as const;
const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, size = 64): v is string => typeof v === "string" &&
  new RegExp(`^[0-9a-f]{${size}}$`).test(v);
const hash = (v: Buffer | string) => createHash("sha256").update(v).digest("hex");
const fail = (code: string): never => { throw new NextBuildError(code); };
const terminal = (state: StaticNextBuildState) =>
  ["SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED"].includes(state);
function object(bytes: Buffer, code: string): Record<string, unknown> {
  try {
    const value: unknown = JSON.parse(bytes.toString("utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) return fail(code);
    return value as Record<string, unknown>;
  } catch { return fail(code); }
}
function copyScope(s: NextBuildScope): NextBuildScope {
  if (!s || ![s.tenantId, s.organizationId, s.projectId, s.repoKey, s.jobId].every(uuid) ||
      !sha(s.sourceCommit, 40) || !sha(s.sourceTree, 40)) fail("source_conflict");
  return { tenantId: s.tenantId, organizationId: s.organizationId, projectId: s.projectId,
    repoKey: s.repoKey, jobId: s.jobId, sourceCommit: s.sourceCommit, sourceTree: s.sourceTree };
}
function mapping(bytes: Buffer, executionId: string, code: string): string {
  const value = object(bytes, code);
  if (value.executionId !== executionId || typeof value.buildId !== "string" || !value.buildId.trim()) fail(code);
  return value.buildId as string;
}
function receipt(bytes: Buffer, input: StaticNextJobInput, state: StaticNextBuildState): StaticNextJobResult {
  const value = object(bytes, "producer_conflict");
  if (value.protocol !== protocol || value.executionId !== input.executionId ||
      value.bindingId !== input.bindingId || value.toolchainSha256 !== input.toolchainSha256 ||
      value.bundleSha256 !== input.bundleSha256 ||
      ![0, 10, 20, 30, 40, 50].includes(value.exitCode as number) ||
      (value.diagnostic !== undefined && (typeof value.diagnostic !== "string" ||
        Buffer.byteLength(value.diagnostic) > 2048))) fail("producer_conflict");
  const files: StaticFile[] = [];
  if (value.exitCode === 0) {
    if (state !== "SUCCEEDED" || !Array.isArray(value.files) || value.files.length > 4096) fail("producer_conflict");
    const paths = new Set<string>();
    let encodedLength = 0;
    for (const file of value.files as unknown[]) {
      if (!file || typeof file !== "object") fail("producer_conflict");
      const f = file as Record<string, unknown>;
      if (typeof f.path !== "string" || f.path.length > 2048 ||
          !/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(f.path) ||
          f.path.split("/").some(part => part === "." || part === "..") ||
          paths.has(f.path) ||
          typeof f.contentType !== "string" || typeof f.body !== "string") fail("producer_conflict");
      paths.add(f.path as string);
      encodedLength += (f.body as string).length;
      if (encodedLength > 4 * Math.ceil(32 * 1024 * 1024 / 3)) fail("producer_conflict");
    }
    for (const file of value.files as Record<string, unknown>[]) {
      const f = file;
      const body = Buffer.from(f.body as string, "base64");
      if (body.toString("base64") !== f.body) fail("producer_conflict");
      files.push({ path: f.path as string, body, contentType: f.contentType as string });
    }
  }
  return { protocol, executionId: input.executionId, bindingId: input.bindingId,
    toolchainSha256: input.toolchainSha256, bundleSha256: input.bundleSha256,
    exitCode: value.exitCode as number, files };
}

/** Inert host transport. Isolation is supplied by the injected build provider. */
export function createCodeBuildStaticNextExecutor(options: CodeBuildStaticNextExecutorOptions): StaticNextJobExecutor {
  const pollMs = options?.pollMs ?? 5000;
  const deadlineMs = options?.deadlineMs ?? 20 * 60 * 1000;
  if (!options || !uuid(options.bindingId) || !sha(options.toolchainSha256) ||
      typeof options.store?.putIfAbsent !== "function" || typeof options.store?.get !== "function" ||
      typeof options.provider?.start !== "function" || typeof options.provider?.state !== "function" ||
      typeof options.provider?.stop !== "function" || !Number.isFinite(pollMs) || pollMs < 100 || pollMs > 60000 ||
      !Number.isFinite(deadlineMs) || deadlineMs <= 0 || deadlineMs > 60 * 60 * 1000 ||
      (options.now !== undefined && typeof options.now !== "function") ||
      (options.sleep !== undefined && typeof options.sleep !== "function")) fail("isolated_next_binding_unavailable");
  const { bindingId, toolchainSha256, store, provider } = options;
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? (ms => new Promise<void>(resolve => setTimeout(resolve, ms)));
  async function stopQuietly(buildId: string): Promise<void> {
    try { await provider.stop(buildId); } catch { /* Preserve the original failure. */ }
  }
  async function state(buildId: string): Promise<StaticNextBuildState> {
    const value = await provider.state(buildId);
    if (!terminal(value) && value !== "QUEUED" && value !== "IN_PROGRESS") fail("build_failed");
    return value;
  }
  return {
    bindingId, toolchainSha256, isolation: "filesystem-and-network",
    async run(value: StaticNextJobInput) {
      try {
        if (!value || value.protocol !== protocol || value.bindingId !== bindingId ||
            value.toolchainSha256 !== toolchainSha256 || !isDeepStrictEqual(value.plan, NEXT_PLAN) ||
            !Buffer.isBuffer(value.bundle) || value.bundle.length < 1 || value.bundle.length > 64 * 1024 * 1024 ||
            !sha(value.bundleSha256) || hash(value.bundle) !== value.bundleSha256) fail("source_conflict");
        const scope = copyScope(value.scope);
        if (value.executionId !== nextRequestDigest(scope)) fail("source_conflict");
        // Snapshot every field before the first asynchronous port call.
        const input: StaticNextJobInput = { protocol, executionId: value.executionId, bindingId,
          toolchainSha256, bundleSha256: value.bundleSha256, scope,
          bundle: Buffer.from(value.bundle), plan: NEXT_PLAN };
        const bytes = Buffer.from(JSON.stringify({ protocol, executionId: input.executionId, bindingId,
          toolchainSha256, bundleSha256: input.bundleSha256, scope,
          bundle: input.bundle.toString("base64"), plan: NEXT_PLAN }));
        const inputKey = `in/${input.executionId}.json`;
        const admitted = await store.putIfAbsent(inputKey, bytes);
        if (admitted === "exists") {
          const prior = await store.get(inputKey);
          if (!prior || !prior.equals(bytes)) fail("job_conflict");
        } else if (admitted !== "created") fail("job_conflict");
        const mapKey = `map/${input.executionId}.json`;
        const prior = await store.get(mapKey);
        let buildId: string;
        if (prior !== null) buildId = mapping(prior, input.executionId, "job_conflict");
        else {
          buildId = await provider.start(input.executionId);
          if (typeof buildId !== "string" || !buildId.trim()) fail("build_failed");
          try {
            const mapped = await store.putIfAbsent(mapKey, Buffer.from(JSON.stringify({ executionId: input.executionId, buildId })));
            if (mapped === "exists") {
              const stored = await store.get(mapKey);
              if (stored === null) fail("job_conflict");
              const winner = mapping(stored!, input.executionId, "job_conflict");
              if (winner !== buildId) await provider.stop(buildId);
              buildId = winner;
            } else if (mapped !== "created") fail("job_conflict");
          } catch (error) { await stopQuietly(buildId); throw error; }
        }
        try {
          const deadline = now() + deadlineMs;
          for (;;) {
            if (now() >= deadline) fail("build_failed");
            const current = await state(buildId);
            if (terminal(current)) {
              const output = await store.get(`out/${input.executionId}.json`);
              if (output === null) fail("build_failed");
              return receipt(output!, input, current);
            }
            await sleep(Math.min(pollMs, Math.max(0, deadline - now())));
          }
        } catch (error) { await stopQuietly(buildId); throw error; }
      } catch (error) {
        if (error instanceof NextBuildError) throw error;
        return fail("build_failed");
      }
    },
    async stop(identity: StaticNextJobIdentity) {
      try {
        if (!identity || identity.protocol !== protocol || identity.bindingId !== bindingId ||
            !sha(identity.executionId)) fail("cleanup_required");
        const executionId = identity.executionId;
        const result = (confirmed: boolean) => ({ protocol, executionId, bindingId, confirmed });
        const stored = await store.get(`map/${executionId}.json`);
        if (stored === null) return result(await store.get(`in/${executionId}.json`) === null);
        const buildId = mapping(stored, executionId, "cleanup_required");
        const deadline = now() + deadlineMs;
        if (terminal(await state(buildId))) return result(true);
        await provider.stop(buildId);
        while (now() < deadline) {
          if (terminal(await state(buildId))) return result(true);
          await sleep(Math.min(pollMs, Math.max(0, deadline - now())));
        }
        return result(false);
      } catch { return fail("cleanup_required"); }
    },
  };
}
