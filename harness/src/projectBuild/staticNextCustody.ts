/** Host-owned durable custody. Transport output alone is never a READY view. */
import { createHash } from "node:crypto";
import type { ProjectRepositoryAuthority } from "../ports/index.js";
import type { StaticNextObjectStore } from "./codebuildStaticNextExecutor.js";
import type { StaticNextJobResult, StaticNextStopResult } from "./managedNextRuntime.js";
import { NextBuildError, nextRequestDigest, staticManifestDigest } from "./genericNextBuild.js";
import type { NextBuildRequest, NextBuildScope, NextPreview, PrivateStaticArtifacts, StaticFile, StaticManifest } from "./genericNextBuild.js";

export const STATIC_NEXT_CUSTODY_SCHEMA = "leaf.static-next-custody.v1" as const;
export interface StaticNextCustodyAdmission extends NextBuildRequest {
  sourceRequestId: string;
  version: number;
  receiptDigest: string;
}
export interface StaticNextCustodyReceipt {
  schema: typeof STATIC_NEXT_CUSTODY_SCHEMA;
  authority: ProjectRepositoryAuthority;
  request: StaticNextCustodyAdmission;
  executionId: string;
  bindingId: string;
  toolchainSha256: string;
  bundleSha256: string;
  providerBuildId: string;
  terminalState: "READY";
  sandboxCleanupConfirmed: true;
  artifactBindingId: string;
  artifactId: string;
  manifestSha256: string;
  manifest: StaticManifest;
  createdAt: string;
  cleanupAt: string;
}
export interface StaticNextCleanupMarker {
  schema: typeof STATIC_NEXT_CUSTODY_SCHEMA;
  executionId: string;
  bundleSha256: string;
  cleanedAt: string;
  filesRemoved: boolean;
}
export interface PrivateStaticViewRecord {
  schema: "leaf.private-static-view.v1";
  authority: ProjectRepositoryAuthority;
  request: StaticNextCustodyAdmission;
  executionId: string;
  providerBuildId: string;
  terminalState: "READY";
  sandboxCleanupConfirmed: true;
  cleaned: false;
  preview: NextPreview;
}

const MAX_RECORD = 2 * 1024 * 1024;
const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, n = 64): v is string => typeof v === "string" &&
  new RegExp(`^[0-9a-f]{${n}}$`).test(v);
const fail = (code = "producer_conflict"): never => { throw new NextBuildError(code); };
const bytes = (value: unknown) => Buffer.from(JSON.stringify(value));
const location = (bundle: string, execution: string) => `static-next/${bundle}/${execution}`;
const hash = (body: Buffer) => createHash("sha256").update(body).digest("hex");

function admission(authority: ProjectRepositoryAuthority, request: StaticNextCustodyAdmission) {
  if (!authority || ![authority.tenantId, authority.organizationId, authority.projectId, authority.repoKey].every(uuid) ||
      !request || !uuid(request.jobId) || !uuid(request.sourceRequestId) ||
      !Number.isSafeInteger(request.version) || request.version < 1 || !sha(request.receiptDigest) ||
      !sha(request.sourceCommit, 40) || !sha(request.sourceTree, 40)) fail();
  const a = { tenantId: authority.tenantId, organizationId: authority.organizationId,
    projectId: authority.projectId, repoKey: authority.repoKey };
  const r = { jobId: request.jobId, sourceRequestId: request.sourceRequestId, version: request.version,
    receiptDigest: request.receiptDigest, sourceCommit: request.sourceCommit, sourceTree: request.sourceTree };
  const scope: NextBuildScope = { ...a, jobId: r.jobId, sourceCommit: r.sourceCommit, sourceTree: r.sourceTree };
  return { authority: a, request: r, scope, executionId: nextRequestDigest(scope) };
}

function manifestCopy(m: StaticManifest): StaticManifest {
  if (!m || !sha(m.requestDigest) || !sha(m.bundleSha256) || !uuid(m.bindingId) ||
      !sha(m.toolchainSha256) || !Array.isArray(m.files) || m.files.length < 1 || m.files.length > 4096) fail();
  const paths = new Set<string>();
  let total = 0;
  const files = m.files.map(f => {
    if (!f || typeof f.path !== "string" || f.path.length > 2048 ||
        !/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(f.path) ||
        f.path.split("/").some(p => p === "." || p === "..") || paths.has(f.path) ||
        !sha(f.sha256) || !Number.isSafeInteger(f.sizeBytes) || f.sizeBytes < 0 || f.sizeBytes > MAX_RECORD ||
        typeof f.contentType !== "string" || f.contentType.length > 256 ||
        !/^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(f.contentType)) fail();
    paths.add(f.path);
    total += f.sizeBytes;
    if (total > 32 * 1024 * 1024) fail();
    return { path: f.path, sha256: f.sha256, sizeBytes: f.sizeBytes, contentType: f.contentType };
  });
  if (!paths.has("/index.html")) fail();
  return { requestDigest: m.requestDigest, bundleSha256: m.bundleSha256,
    bindingId: m.bindingId, toolchainSha256: m.toolchainSha256, files };
}

function view(r: StaticNextCustodyReceipt): PrivateStaticViewRecord {
  return { schema: "leaf.private-static-view.v1", authority: r.authority, request: r.request,
    executionId: r.executionId, providerBuildId: r.providerBuildId, terminalState: "READY",
    sandboxCleanupConfirmed: true, cleaned: false, preview: {
      jobId: r.request.jobId, sourceCommit: r.request.sourceCommit, sourceTree: r.request.sourceTree,
      bundleSha256: r.bundleSha256, bindingId: r.bindingId, toolchainSha256: r.toolchainSha256,
      artifactBindingId: r.artifactBindingId, artifactId: r.artifactId,
      manifestSha256: r.manifestSha256, manifest: r.manifest,
    } };
}

function utc(value: string): boolean {
  const time = Date.parse(value);
  return typeof value === "string" && Number.isFinite(time) && new Date(time).toISOString() === value;
}

function receiptCopy(r: StaticNextCustodyReceipt): StaticNextCustodyReceipt {
  if (!r) fail();
  const a = admission(r.authority, r.request);
  const manifest = manifestCopy(r.manifest);
  if (r.schema !== STATIC_NEXT_CUSTODY_SCHEMA || r.executionId !== a.executionId ||
      r.terminalState !== "READY" || r.sandboxCleanupConfirmed !== true ||
      !uuid(r.bindingId) || !uuid(r.artifactBindingId) || !uuid(r.artifactId) ||
      !sha(r.bundleSha256) || !sha(r.toolchainSha256) ||
      typeof r.providerBuildId !== "string" || !r.providerBuildId.trim() || r.providerBuildId.length > 512 ||
      manifest.requestDigest !== r.executionId || manifest.bundleSha256 !== r.bundleSha256 ||
      manifest.bindingId !== r.bindingId || manifest.toolchainSha256 !== r.toolchainSha256 ||
      r.manifestSha256 !== staticManifestDigest(manifest) || !utc(r.createdAt) || !utc(r.cleanupAt) ||
      Date.parse(r.cleanupAt) <= Date.parse(r.createdAt)) fail();
  const result = { ...r, authority: a.authority, request: a.request, manifest };
  if (bytes(result).length > MAX_RECORD || bytes(view(result)).length > MAX_RECORD) fail();
  return result;
}

export async function publishStaticNextCustody(options: {
  store: StaticNextObjectStore; artifacts: PrivateStaticArtifacts;
  authority: ProjectRepositoryAuthority; request: StaticNextCustodyAdmission;
  result: StaticNextJobResult; providerBuildId: string; stop: StaticNextStopResult;
  now: () => Date; retentionMs: number;
}): Promise<StaticNextCustodyReceipt> {
  const { store, artifacts, providerBuildId } = options;
  const a = admission(options.authority, options.request);
  const r = options.result;
  const stop = options.stop;
  if (!r || r.protocol !== "leaf.static-next.v1" || r.exitCode !== 0 || r.executionId !== a.executionId ||
      !stop || stop.protocol !== r.protocol || stop.confirmed !== true ||
      stop.executionId !== a.executionId || stop.bindingId !== r.bindingId ||
      !artifacts || !uuid(artifacts.bindingId) || artifacts.access !== "private-authenticated" ||
      artifacts.immutable !== true || !Number.isSafeInteger(options.retentionMs) || options.retentionMs <= 0 ||
      !Array.isArray(r.files) || r.files.length < 1 || r.files.length > 4096) fail();
  const files: StaticFile[] = r.files.map(f => {
    if (!f || !Buffer.isBuffer(f.body) || f.body.length > MAX_RECORD) fail();
    return { path: f.path, contentType: f.contentType, body: Buffer.from(f.body) };
  });
  const manifest = manifestCopy({ requestDigest: a.executionId, bundleSha256: r.bundleSha256,
    bindingId: r.bindingId, toolchainSha256: r.toolchainSha256, files: files.map(f => ({
      path: f.path, contentType: f.contentType, sizeBytes: f.body.length, sha256: hash(f.body),
    })) });
  const created = options.now();
  const cleanup = new Date(created.getTime() + options.retentionMs);
  if (!Number.isFinite(created.getTime()) || !Number.isFinite(cleanup.getTime())) fail();
  // Validate all locally knowable preconditions, including metadata bounds,
  // before either persistence port is allowed to write.
  const draft = receiptCopy({ schema: STATIC_NEXT_CUSTODY_SCHEMA, authority: a.authority, request: a.request,
    executionId: a.executionId, bindingId: manifest.bindingId, toolchainSha256: manifest.toolchainSha256,
    bundleSha256: manifest.bundleSha256, providerBuildId, terminalState: "READY", sandboxCleanupConfirmed: true,
    artifactBindingId: artifacts.bindingId, artifactId: "00000000-0000-0000-0000-000000000000",
    manifestSha256: staticManifestDigest(manifest), manifest,
    createdAt: created.toISOString(), cleanupAt: cleanup.toISOString() });
  const key = `${location(draft.bundleSha256, a.executionId)}/receipt.json`;
  const prior = await store.get(key);
  if (prior !== null) {
    if (prior.length > MAX_RECORD) fail("job_conflict");
    let existing: StaticNextCustodyReceipt;
    try { existing = receiptCopy(JSON.parse(prior.toString("utf8"))); }
    catch { return fail("job_conflict"); }
    // The existing artifact ID is only used to compare immutable receipt bytes;
    // publication still requires the real store acknowledgement below.
    if (!prior.equals(bytes({ ...draft, artifactId: existing.artifactId }))) fail("job_conflict");
  }
  const stored = await artifacts.put(a.scope, manifest, files);
  if (!stored || !uuid(stored.artifactId) || stored.manifestSha256 !== draft.manifestSha256) fail();
  const receipt = receiptCopy({ ...draft, artifactId: stored.artifactId });
  const body = bytes(receipt);
  const status = await store.putIfAbsent(key, body);
  if (status === "exists") {
    const existing = await store.get(key);
    if (!existing || !existing.equals(body)) fail("job_conflict");
  } else if (status !== "created") fail("job_conflict");
  return receipt;
}

export async function markStaticNextCleaned(options: {
  store: StaticNextObjectStore; bundleSha256: string; executionId: string;
  filesRemoved: boolean; now: () => Date;
}): Promise<void> {
  if (!sha(options.bundleSha256) || !sha(options.executionId) || typeof options.filesRemoved !== "boolean") fail();
  const time = options.now();
  if (!Number.isFinite(time.getTime())) fail();
  const marker: StaticNextCleanupMarker = { schema: STATIC_NEXT_CUSTODY_SCHEMA,
    executionId: options.executionId, bundleSha256: options.bundleSha256,
    cleanedAt: time.toISOString(), filesRemoved: options.filesRemoved };
  await options.store.putIfAbsent(`${location(options.bundleSha256, options.executionId)}/cleaned.json`, bytes(marker));
}

export async function loadPrivateStaticView(options: {
  store: StaticNextObjectStore; artifacts: PrivateStaticArtifacts;
  authority: ProjectRepositoryAuthority; request: StaticNextCustodyAdmission;
  bundleSha256: string; now: () => Date;
}): Promise<PrivateStaticViewRecord | null> {
  try {
    const a = admission(options.authority, options.request);
    if (!sha(options.bundleSha256)) return null;
    const base = location(options.bundleSha256, a.executionId);
    const raw = await options.store.get(`${base}/receipt.json`);
    if (!raw || raw.length > MAX_RECORD) return null;
    const r = receiptCopy(JSON.parse(raw.toString("utf8")));
    if (r.executionId !== a.executionId || r.bundleSha256 !== options.bundleSha256 ||
        r.artifactBindingId !== options.artifacts.bindingId ||
        !bytes(r.authority).equals(bytes(a.authority)) || !bytes(r.request).equals(bytes(a.request))) return null;
    // Only null is a confirmed absence. Any marker bytes, even malformed, fence serving.
    if (await options.store.get(`${base}/cleaned.json`) !== null) return null;
    const now = options.now().getTime();
    if (!Number.isFinite(now) || now >= Date.parse(r.cleanupAt)) return null;
    return view(r);
  } catch { return null; }
}
