/** Durable custody for static bytes. The trusted composition root owns this
 * dedicated directory and the current-access callback. This adapter executes
 * no content and supplies no HTTP origin or managed build runtime.
 */
import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, rename, rm } from "node:fs/promises";
import { isAbsolute, join, parse, resolve } from "node:path";
import { NextBuildError, nextRequestDigest, staticManifestDigest } from "./genericNextBuild.js";
import type { NextBuildScope, PrivateStaticArtifacts, StaticFile, StaticManifest } from "./genericNextBuild.js";

const MAX_FILE = 2 * 1024 * 1024;
const MAX_TOTAL = 32 * 1024 * 1024;
const MAX_METADATA = 16 * 1024 * 1024;
const uuid = (v: unknown): v is string => typeof v === "string" &&
  /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/.test(v);
const sha = (v: unknown, n = 64): v is string => typeof v === "string" &&
  new RegExp(`^[0-9a-f]{${n}}$`).test(v);
const hash = (v: Buffer | string) => createHash("sha256").update(v).digest("hex");
const safePath = (v: unknown): v is string => typeof v === "string" && v.length <= 2048 &&
  /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(v) &&
  !v.split("/").some(p => p === "." || p === "..");
const mediaType = (v: unknown): v is string => typeof v === "string" &&
  v.length <= 256 && /^[a-z0-9.+-]+\/[a-z0-9.+-]+(?:; charset=utf-8)?$/i.test(v);
const conflict = (): never => { throw new NextBuildError("artifact_conflict"); };
const code = (e: unknown) => (e as NodeJS.ErrnoException)?.code;
const artifactId = (digest: string) =>
  `${digest.slice(0, 8)}-${digest.slice(8, 12)}-${digest.slice(12, 16)}-${digest.slice(16, 20)}-${digest.slice(20, 32)}`;

function scopeCopy(value: NextBuildScope): NextBuildScope {
  if (!value || ![value.tenantId, value.organizationId, value.projectId, value.repoKey, value.jobId].every(uuid) ||
      !sha(value.sourceCommit, 40) || !sha(value.sourceTree, 40)) conflict();
  return Object.freeze({ tenantId: value.tenantId, organizationId: value.organizationId,
    projectId: value.projectId, repoKey: value.repoKey, jobId: value.jobId,
    sourceCommit: value.sourceCommit, sourceTree: value.sourceTree });
}

function manifestCopy(scope: NextBuildScope, value: StaticManifest): StaticManifest {
  if (!value || value.requestDigest !== nextRequestDigest(scope) || !sha(value.bundleSha256) ||
      !uuid(value.bindingId) || !sha(value.toolchainSha256) || !Array.isArray(value.files) ||
      value.files.length < 1 || value.files.length > 4096) conflict();
  let total = 0;
  const paths = new Set<string>();
  const files = value.files.map(f => {
    if (!f || !safePath(f.path) || paths.has(f.path) || !sha(f.sha256) ||
        !Number.isSafeInteger(f.sizeBytes) || f.sizeBytes < 0 || f.sizeBytes > MAX_FILE ||
        !mediaType(f.contentType)) conflict();
    paths.add(f.path);
    total += f.sizeBytes;
    if (total > MAX_TOTAL) conflict();
    return Object.freeze({ path: f.path, sha256: f.sha256, sizeBytes: f.sizeBytes, contentType: f.contentType });
  });
  if (!paths.has("/index.html")) conflict();
  return Object.freeze({ requestDigest: value.requestDigest, bundleSha256: value.bundleSha256,
    bindingId: value.bindingId, toolchainSha256: value.toolchainSha256, files: Object.freeze(files) });
}

export class DiskPrivateStaticArtifacts implements PrivateStaticArtifacts {
  readonly access = "private-authenticated" as const;
  readonly immutable = true as const;
  readonly bindingId: string;
  private readonly root: string;
  private readonly isCurrent: (scope: NextBuildScope) => Promise<boolean>;

  constructor(options: {
    root: string; bindingId: string; isCurrent(scope: NextBuildScope): Promise<boolean>;
  }) {
    if (!options || typeof options.root !== "string" || !isAbsolute(options.root) ||
        resolve(options.root) === parse(resolve(options.root)).root || !uuid(options.bindingId) ||
        typeof options.isCurrent !== "function") throw new NextBuildError("private_artifact_binding_unavailable");
    this.root = resolve(options.root);
    this.bindingId = options.bindingId;
    this.isCurrent = options.isCurrent;
  }

  private async current(scope: NextBuildScope): Promise<void> {
    let allowed: boolean;
    try { allowed = await this.isCurrent(scope); }
    catch { throw new NextBuildError("source_unavailable"); }
    if (allowed !== true) throw new NextBuildError("source_stale");
  }

  /** Every component is checked; caller asset paths never become disk paths.
   * The dedicated root must remain owned by the trusted host, not tenant code.
   */
  private async directory(path: string, create = false): Promise<void> {
    const root = parse(path).root;
    let cursor = root;
    for (const part of path.slice(root.length).split(/[\\/]/).filter(Boolean)) {
      cursor = join(cursor, part);
      if (create) await mkdir(cursor, { mode: 0o700 }).catch(e => { if (code(e) !== "EEXIST") throw e; });
      const info = await lstat(cursor);
      if (info.isSymbolicLink() || !info.isDirectory()) conflict();
    }
  }

  private base(): string { return join(this.root, this.bindingId); }
  private location(scope: NextBuildScope): string { return join(this.base(), nextRequestDigest(scope)); }
  private asset(path: string): string { return hash(path) + ".asset"; }

  private async readBytes(scope: NextBuildScope, path: string, maxBytes: number): Promise<Buffer> {
    await this.current(scope);
    const entry = await lstat(path);
    if (entry.isSymbolicLink() || !entry.isFile() || entry.size > maxBytes) conflict();
    const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    let body: Buffer;
    try {
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > maxBytes || stat.dev !== entry.dev || stat.ino !== entry.ino) conflict();
      // Bounded allocation even if a damaged file grows while being read.
      const buffer = Buffer.alloc(stat.size + 1);
      let length = 0;
      while (length < buffer.length) {
        const result = await handle.read(buffer, length, buffer.length - length, length);
        if (!result.bytesRead) break;
        length += result.bytesRead;
      }
      if (length !== stat.size || length > maxBytes) conflict();
      body = Buffer.from(buffer.subarray(0, length));
    } finally { await handle.close(); }
    await this.current(scope);
    return body;
  }

  private async stored(scope: NextBuildScope): Promise<StaticManifest> {
    const dir = this.location(scope);
    await this.directory(dir);
    const raw = await this.readBytes(scope, join(dir, "manifest.json"), MAX_METADATA);
    let row: { scope: NextBuildScope; bindingId: string; manifest: StaticManifest; manifestSha256: string };
    try { row = JSON.parse(raw.toString("utf8")) as typeof row; }
    catch { return conflict(); }
    if (!row || row.bindingId !== this.bindingId ||
        JSON.stringify(scopeCopy(row.scope)) !== JSON.stringify(scope)) conflict();
    const manifest = manifestCopy(scope, row.manifest);
    if (row.manifestSha256 !== staticManifestDigest(manifest)) conflict();
    return manifest;
  }

  private async verify(scope: NextBuildScope, expected: StaticManifest): Promise<void> {
    const manifest = await this.stored(scope);
    if (staticManifestDigest(manifest) !== staticManifestDigest(expected)) conflict();
    const names = (await readdir(this.location(scope))).sort();
    const expectedNames = ["manifest.json", ...manifest.files.map(f => this.asset(f.path))].sort();
    if (JSON.stringify(names) !== JSON.stringify(expectedNames)) conflict();
    for (const file of manifest.files) {
      const body = await this.readBytes(scope, join(this.location(scope), this.asset(file.path)), file.sizeBytes);
      if (body.length !== file.sizeBytes || hash(body) !== file.sha256) conflict();
    }
  }

  private async write(path: string, body: Buffer): Promise<void> {
    const handle = await open(path, "wx", 0o600);
    try { await handle.writeFile(body); await handle.sync(); }
    finally { await handle.close(); }
  }

  private async syncDirectory(path: string): Promise<void> {
    // POSIX directory barriers persist publication metadata. Node cannot open
    // directories for fsync on Windows; file fsync and atomic rename still apply.
    let handle;
    try { handle = await open(path, "r"); }
    catch (e) {
      if (process.platform === "win32" && ["EPERM", "EISDIR", "EACCES"].includes(code(e) ?? "")) return;
      throw e;
    }
    try { await handle.sync(); }
    finally { await handle.close(); }
  }

  async put(value: NextBuildScope, input: StaticManifest, supplied: readonly StaticFile[]) {
    // Snapshot before the first await so caller mutations cannot change custody.
    const scope = scopeCopy(value);
    const manifest = manifestCopy(scope, input);
    if (!Array.isArray(supplied) || supplied.length !== manifest.files.length) conflict();
    const files = new Map<string, Buffer>();
    const descriptors = new Map(manifest.files.map(f => [f.path, f]));
    for (const f of supplied) {
      const descriptor = f && descriptors.get(f.path);
      if (!descriptor || files.has(f.path) || !Buffer.isBuffer(f.body) || f.body.length !== descriptor.sizeBytes ||
          f.contentType !== descriptor.contentType || hash(f.body) !== descriptor.sha256) conflict();
      files.set(f.path, Buffer.from(f.body));
    }
    const manifestSha256 = staticManifestDigest(manifest);
    const identity = artifactId(manifestSha256);
    const envelope = Buffer.from(JSON.stringify({ scope, bindingId: this.bindingId, manifest, manifestSha256 }));
    if (envelope.length > MAX_METADATA) conflict();
    await this.current(scope);
    await this.directory(this.base(), true);
    const destination = this.location(scope);
    let exists = false;
    try { await lstat(destination); exists = true; }
    catch (e) { if (code(e) !== "ENOENT") throw e; }
    if (exists) {
      await this.verify(scope, manifest);
      await this.current(scope);
      return { artifactId: identity, manifestSha256 };
    }

    const stage = join(this.base(), `.${nextRequestDigest(scope)}.${randomUUID()}.pending`);
    await mkdir(stage, { mode: 0o700 });
    try {
      for (const file of manifest.files) await this.write(join(stage, this.asset(file.path)), files.get(file.path)!);
      await this.current(scope);
      // The commit record is written only after every immutable asset is synced.
      await this.write(join(stage, "manifest.json"), envelope);
      await this.syncDirectory(stage);
      await this.current(scope);
      try { await rename(stage, destination); }
      catch (e) {
        if (!["EEXIST", "ENOTEMPTY", "EPERM", "EACCES"].includes(code(e) ?? "")) throw e;
        await this.verify(scope, manifest);
      }
      await this.syncDirectory(this.base());
      await this.current(scope);
      return { artifactId: identity, manifestSha256 };
    } finally { await rm(stage, { recursive: true, force: true }); }
  }

  async read(value: NextBuildScope, id: string, path: string, maxBytes: number) {
    const scope = scopeCopy(value);
    const assetPath = path === "/" ? "/index.html" : path;
    if (!uuid(id)) throw new NextBuildError("preview_unavailable");
    if (!safePath(assetPath)) throw new NextBuildError("preview_path_invalid");
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 0 || maxBytes > MAX_FILE) conflict();
    await this.current(scope);
    let manifest: StaticManifest;
    try { manifest = await this.stored(scope); }
    catch (e) {
      if (code(e) === "ENOENT") throw new NextBuildError("preview_unavailable");
      throw e;
    }
    if (id !== artifactId(staticManifestDigest(manifest))) throw new NextBuildError("preview_unavailable");
    const file = manifest.files.find(f => f.path === assetPath);
    if (!file) throw new NextBuildError("preview_unavailable");
    if (file.sizeBytes > maxBytes) conflict();
    const body = await this.readBytes(scope, join(this.location(scope), this.asset(file.path)), file.sizeBytes);
    if (body.length !== file.sizeBytes || hash(body) !== file.sha256) conflict();
    await this.current(scope);
    return { body, contentType: file.contentType };
  }
}
