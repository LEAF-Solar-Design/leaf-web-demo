import { execFileSync, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, realpathSync } from "node:fs";
import { join } from "node:path";
import type { HarnessIdentity } from "../index.js";
import { LAB_DOCUMENT_REF, LAB_GENERATION_REF, LAB_TRANSITIONS_PREFIX, GitGenerationConflictError, LabHistoryError } from "../labDocumentPort.js";
import type { LabDocumentInspection, LabDocumentPort, LabDocumentWrite, LabGenerationFence, LabGenerationReceipt, LabChangesRequest, LabHistoryCommit, LabOpIdentity } from "../labDocumentPort.js";
import type { PgTenantRepoLeaseCoordinator } from "./tenantRepoProvider.js";
import { ForgePublicationError, readLabGeneration, validLabGeneration as validGeneration } from "./forgeRemoteAuthority.js";
import type { ForgeRemoteAuthority } from "./forgeRemoteAuthority.js";
import { GitRefConflictError } from "./tenantChangeRepo.js";

const SHA = /^[0-9a-f]{40}$(?![\s\S])/;
const PATH = /^lab\/(graphs|widgets)\/[A-Za-z0-9][A-Za-z0-9_-]*\.json$(?![\s\S])/;
const ZERO = "0".repeat(40);
const OP = /^[0-9a-f]{32}$(?![\s\S])/;
const PAGE_BYTES = 1024 * 1024;

function historyTrailers(body: string): Map<string, string[]> {
  const paragraph = body.replace(/(?:\r?\n)+$/, "").split(/\r?\n\r?\n/).at(-1) ?? "";
  const result = new Map<string, string[]>();
  for (const line of paragraph.split(/\r?\n/)) {
    const match = /^([A-Za-z0-9-]+): ([^\r\n]*)$/.exec(line);
    if (match) result.set(match[1]!, [...(result.get(match[1]!) ?? []), match[2]!]);
  }
  return result;
}

export interface TenantDocumentRepoOptions {
  /** Server-owned mapping only. No repository path or URL comes from a write request. */
  locate(tenantId: string): Promise<{ dir: string; sourceRef: string }>;
  lease: Pick<PgTenantRepoLeaseCoordinator, "withLease" | "runFenced">;
  authority: ForgeRemoteAuthority;
  identity: HarnessIdentity;
  maxHistory?: number;
}

/** No checkout, index, filters, hooks or worktree. Only Git objects and the Lab ref. */
export class TenantDocumentRepo implements LabDocumentPort {
  constructor(private readonly opts: TenantDocumentRepoOptions) {
    if (opts.maxHistory !== undefined && (!Number.isSafeInteger(opts.maxHistory) || opts.maxHistory < 1 || opts.maxHistory > 100000)) {
      throw new LabHistoryError("lab_history_invalid", "invalid history bound");
    }
  }

  protected git(dir: string, args: string[], input?: string): string {
    try { return this.gitRaw(dir, args, input).toString("utf8"); }
    catch { throw new Error("Lab Git operation failed"); }
  }

  private gitRaw(dir: string, args: string[], input?: string): Buffer {
    const invocation = this.gitInvocation(dir, args);
    return execFileSync("git", invocation.args, { cwd: dir, env: invocation.env, input, maxBuffer: 2 * 1024 * 1024, windowsHide: true });
  }

  private gitInvocation(dir: string, args: string[]): { args: string[]; env: NodeJS.ProcessEnv } {
    const env: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (/^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP)$/i.test(key)) env[key] = value;
    }
    Object.assign(env, {
      GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null",
      GIT_NO_REPLACE_OBJECTS: "1", GIT_TERMINAL_PROMPT: "0",
    });
    return { args: [
      "-c", "safe.directory=", "-c", `safe.directory=${realpathSync(dir).replaceAll("\\", "/")}`,
      "-c", `core.hooksPath=${process.platform === "win32" ? "NUL" : "/dev/null"}`,
      "-c", "core.fsmonitor=false",
      "-c", "commit.gpgSign=false", "-c", `user.name=${this.opts.identity.name}`,
      "-c", `user.email=${this.opts.identity.email}`, ...args,
    ], env };
  }

  private verifyHistoryHead(dir: string, head: string): void {
    try {
      if (this.git(dir, ["cat-file", "-t", head]).trim() !== "commit"
        || this.git(dir, ["rev-parse", "--is-shallow-repository"]).trim() !== "false") {
        throw new Error("incomplete history or invalid head");
      }
    } catch { throw new LabHistoryError("lab_history_invalid", "invalid or incomplete history head"); }
  }

  /** NUL framing keeps raw messages and unusual path names independent of line parsing. */
  private async *history(dir: string, range: string, paths: boolean): AsyncGenerator<{ entry: LabHistoryCommit; identity: Map<string, string[]> }> {
    const invocation = this.gitInvocation(dir, ["log", "--no-show-signature", "--no-notes", "--no-decorate",
      "--format=%x00%H%x00%P%x00%B", "-z", ...(paths ? ["--reverse", "--root", "--name-only", "--no-renames"] : []), range, "--"]);
    const child = spawn("git", invocation.args, { cwd: dir, env: invocation.env, windowsHide: true, stdio: ["ignore", "pipe", "ignore"] });
    const completion = new Promise<number | null>((resolve) => {
      child.once("error", () => resolve(-1));
      child.once("close", resolve);
    });
    let pending = Buffer.alloc(0);
    let field = 0;
    let entry: LabHistoryCommit | undefined;
    let identity = new Map<string, string[]>();
    let count = 0;
    let firstPath = true;
    try {
      for await (const chunk of child.stdout) {
        const data = Buffer.concat([pending, chunk as Buffer]);
        let start = 0;
        for (let end = data.indexOf(0); end !== -1; end = data.indexOf(0, start)) {
          const token = data.subarray(start, end);
          start = end + 1;
          if (token.length > (field === 3 ? 65536 : PAGE_BYTES)) throw new LabHistoryError("lab_history_invalid");
          const value = token.toString("utf8");
          if (field === 0) {
            if (value !== "") throw new LabHistoryError("lab_history_invalid");
            field = 1;
          } else if (field === 1) {
            if (!SHA.test(value)) throw new LabHistoryError("lab_history_invalid");
            if (++count > (this.opts.maxHistory ?? 100000)) throw new LabHistoryError("lab_history_too_long");
            entry = { commit: value, parent: null, changedPaths: [], trailers: {} };
            field = 2;
          } else if (field === 2) {
            const parents = value ? value.split(" ") : [];
            if (parents.length > 1) throw new LabHistoryError("lab_history_nonlinear");
            if (parents.some((parent) => !SHA.test(parent))) throw new LabHistoryError("lab_history_invalid");
            entry!.parent = parents[0] ?? null;
            field = 3;
          } else if (field === 3) {
            identity = historyTrailers(value);
            entry!.trailers = Object.fromEntries([...identity].map(([key, values]) => [key, values.at(-1)!]));
            field = 4;
            firstPath = true;
          } else if (value === "") {
            yield { entry: entry!, identity };
            entry = undefined;
            field = 1;
          } else {
            if (!paths) throw new LabHistoryError("lab_history_invalid");
            const path = firstPath ? value.replace(/^\n/, "") : value;
            firstPath = false;
            if (!path || entry!.changedPaths.length >= 10000) throw new LabHistoryError("lab_history_invalid");
            entry!.changedPaths.push(path);
          }
        }
        pending = Buffer.from(data.subarray(start));
        if (pending.length > (field === 3 ? 65536 : PAGE_BYTES)) throw new LabHistoryError("lab_history_invalid");
      }
      if (await completion !== 0 || pending.length || (field !== 0 && field !== 4)) throw new LabHistoryError("lab_history_invalid");
      if (entry) yield { entry, identity };
    } finally {
      child.kill();
      await completion;
    }
  }

  async lookupOps(tenantId: string, request: { opIds: string[]; head: string }): Promise<{ ops: Record<string, LabOpIdentity | null> }> {
    if (!SHA.test(request.head) || !Array.isArray(request.opIds) || request.opIds.length < 1 || request.opIds.length > 64
      || request.opIds.some((id) => typeof id !== "string" || !OP.test(id)) || new Set(request.opIds).size !== request.opIds.length) {
      throw new LabHistoryError("lab_history_invalid");
    }
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, async () => {
        this.verifyHistoryHead(dir, request.head);
        const ops: Record<string, LabOpIdentity | null> = Object.fromEntries(request.opIds.map((id) => [id, null]));
        const seen = new Set<string>();
        for await (const { entry, identity } of this.history(dir, request.head, false)) {
          if (!identity.has("Lab-Op")) continue;
          const one = (key: string) => identity.get(key)?.length === 1 ? identity.get(key)![0] : undefined;
          const op = one("Lab-Op"), docId = one("Lab-Doc"), revision = one("Lab-Revision"), sha256 = one("Lab-Sha256");
          if (!op || !OP.test(op) || !docId || !/^[A-Za-z0-9][A-Za-z0-9_-]*$(?![\s\S])/.test(docId)
            || !revision || !/^[1-9][0-9]*$(?![\s\S])/.test(revision) || !Number.isSafeInteger(Number(revision))
            || !sha256 || !/^[0-9a-f]{64}$(?![\s\S])/.test(sha256) || seen.has(op)) {
            throw new LabHistoryError("lab_ops_ambiguous");
          }
          seen.add(op);
          if (Object.hasOwn(ops, op)) ops[op] = { commit: entry.commit, docId, revision: Number(revision), sha256 };
        }
        return { ops };
      });
    });
  }

  async changes(tenantId: string, request: LabChangesRequest): Promise<{ commits: LabHistoryCommit[]; next: string | null }> {
    const limit = request.limit ?? 1000;
    if (!SHA.test(request.head) || (request.since !== null && !SHA.test(request.since))
      || (request.after !== undefined && !SHA.test(request.after)) || !Number.isInteger(limit) || limit < 1 || limit > 1000) {
      throw new LabHistoryError("lab_history_invalid");
    }
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, async () => {
        this.verifyHistoryHead(dir, request.head);
        if (request.since !== null) {
          try { this.git(dir, ["merge-base", "--is-ancestor", request.since, request.head]); }
          catch { throw new LabHistoryError("lab_history_not_ancestor"); }
        }
        const commits: LabHistoryCommit[] = [];
        let foundAfter = request.after === undefined;
        let more = false;
        let bytes = Buffer.byteLength(JSON.stringify({ commits: [], next: "0".repeat(40) }));
        const range = request.since === null ? request.head : `${request.since}..${request.head}`;
        for await (const { entry } of this.history(dir, range, true)) {
          if (!foundAfter) { if (entry.commit === request.after) foundAfter = true; continue; }
          if (more) continue;
          const size = Buffer.byteLength(JSON.stringify(entry)) + (commits.length ? 1 : 0);
          if (commits.length >= limit || bytes + size > PAGE_BYTES) {
            if (!commits.length) throw new LabHistoryError("lab_history_invalid", "commit exceeds page bound");
            more = true;
          } else { commits.push(entry); bytes += size; }
        }
        if (!foundAfter) throw new LabHistoryError("lab_history_invalid", "after is outside history range");
        return { commits, next: more ? commits.at(-1)!.commit : null };
      });
    });
  }

  async inspect(tenantId: string, options: { readonly limit: number }): Promise<LabDocumentInspection> {
    if (!Number.isInteger(options.limit) || options.limit < 1 || options.limit > 200) throw new Error("invalid inspect limit");
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, async () => {
        const head = this.readRef(dir);
        if (!head) return { ref: LAB_DOCUMENT_REF, head: null, commits: [], documents: [] };
        const commits: LabDocumentInspection["commits"][number][] = [];
        for (const commit of this.git(dir, ["rev-list", "--first-parent", `--max-count=${options.limit}`, head]).trim().split("\n")) {
          if (!SHA.test(commit)) throw new Error("invalid Lab commit");
          const raw = this.gitRaw(dir, ["cat-file", "commit", commit]);
          const separator = raw.indexOf("\n\n");
          if (separator < 0) throw new Error("invalid Lab commit headers");
          const message = raw.subarray(separator + 2);
          if (message.length > 16 * 1024) throw new Error("Lab commit message too large");
          const parents = raw.subarray(0, separator).toString("utf8").split("\n").filter(line => line.startsWith("parent ")).map(line => line.slice(7));
          if (parents.length > 1 || parents.some(parent => !SHA.test(parent))) throw new Error("invalid Lab commit parents");
          commits.push({ commit, parent: parents[0] ?? null, message: message.toString("utf8") });
        }
        const entries = this.git(dir, ["ls-tree", "-rz", "--full-tree", head]).split("\0").filter(Boolean);
        if (entries.length > 2000) throw new Error("too many Lab documents");
        const documents: LabDocumentInspection["documents"][number][] = [];
        for (const entry of entries) {
          const match = /^100644 blob ([0-9a-f]{40})\t(.+)$/.exec(entry);
          if (!match || !PATH.test(match[2]!)) throw new Error("Lab tree contains a forbidden path or mode");
          const blob = match[1]!;
          const size = Number(this.git(dir, ["cat-file", "-s", blob]).trim());
          if (!Number.isSafeInteger(size) || size < 0 || size > 1024 * 1024) throw new Error("Lab blob too large");
          const bytes = this.gitRaw(dir, ["cat-file", "blob", blob]);
          if (bytes.length !== size) throw new Error("invalid Lab blob size");
          documents.push({ path: match[2]!, blob, sha256: createHash("sha256").update(bytes).digest("hex"), bytes: bytes.length });
        }
        return { ref: LAB_DOCUMENT_REF, head, commits, documents };
      });
    });
  }

  private readRef(dir: string): string | null {
    return this.exactRef(dir, LAB_DOCUMENT_REF);
  }

  private exactRef(dir: string, ref: string): string | null {
    const value = this.git(dir, ["for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)", ref]).trim();
    if (!value) return null;
    const [name, oid] = value.split("\t");
    if (name !== ref || !oid || !SHA.test(oid) || value !== `${ref}\t${oid}`) throw new Error("invalid Lab ref");
    return oid;
  }

  private readGeneration(dir: string): Promise<{ generation: string | null; generationOid: string | null }> {
    return readLabGeneration(async (args) => this.gitRaw(dir, args));
  }

  private readBlob(dir: string, oid: string): string {
    if (this.git(dir, ["cat-file", "-t", oid]).trim() !== "blob") throw new GitGenerationConflictError("invalid Lab metadata blob");
    return this.git(dir, ["cat-file", "blob", oid]);
  }

  async generation(tenantId: string): Promise<{ generation: string | null; generationOid: string | null; head: string | null }> {
    return this.opts.lease.withLease(tenantId, async () => {
      const { dir } = await this.mapped(tenantId);
      return { ...await this.readGeneration(dir), head: this.readRef(dir) };
    });
  }

  async fence(tenantId: string, request: LabGenerationFence): Promise<{ receipt: LabGenerationReceipt; head: string | null }> {
    const { transitionId, expectedGeneration, newGeneration, expectedHead } = request;
    if (!/^[0-9a-f]{32}$(?![\s\S])/.test(transitionId) || !validGeneration(newGeneration)
      || (expectedGeneration !== null && !validGeneration(expectedGeneration))
      || (expectedHead !== null && (!SHA.test(expectedHead) || expectedHead === ZERO))) {
      throw new GitGenerationConflictError("invalid Lab fence request");
    }
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, async () => {
        const current = await this.readGeneration(dir);
        const head = this.readRef(dir);
        const receipt: LabGenerationReceipt = { barrier_head: expectedHead, from_generation: expectedGeneration,
          to_generation: newGeneration, transition_id: transitionId };
        const content = JSON.stringify(receipt);
        let maximum = 0n;
        let replay: string | undefined;
        for (const ref of this.git(dir, ["for-each-ref", "--format=%(refname)", LAB_TRANSITIONS_PREFIX]).trim().split(/\r?\n/).filter(Boolean)) {
          const id = ref.slice(LAB_TRANSITIONS_PREFIX.length);
          if (!/^[0-9a-f]{32}$/.test(id)) throw new GitGenerationConflictError("invalid Lab receipt ref");
          const oid = this.exactRef(dir, ref);
          if (!oid) throw new GitGenerationConflictError("missing Lab receipt");
          const raw = this.readBlob(dir, oid);
          let stored: LabGenerationReceipt;
          try { stored = JSON.parse(raw) as LabGenerationReceipt; }
          catch { throw new GitGenerationConflictError("invalid Lab receipt"); }
          if (!stored || stored.transition_id !== id || typeof stored.to_generation !== "string" || !validGeneration(stored.to_generation)
            || (stored.from_generation !== null && (typeof stored.from_generation !== "string" || !validGeneration(stored.from_generation)))
            || (stored.barrier_head !== null && (typeof stored.barrier_head !== "string" || !SHA.test(stored.barrier_head)))
            || raw !== JSON.stringify({ barrier_head: stored.barrier_head, from_generation: stored.from_generation,
              to_generation: stored.to_generation, transition_id: stored.transition_id })) {
            throw new GitGenerationConflictError("invalid Lab receipt");
          }
          if (BigInt(stored.to_generation) > maximum) maximum = BigInt(stored.to_generation);
          if (id === transitionId) replay = raw;
        }
        if (replay !== undefined) {
          if (replay !== content) throw new GitGenerationConflictError("Lab transition already exists");
          return { receipt, head };
        }
        if (current.generation !== expectedGeneration || head !== expectedHead
          || BigInt(newGeneration) <= BigInt(current.generation ?? "0") || BigInt(newGeneration) <= maximum) {
          throw new GitGenerationConflictError();
        }
        const generationOid = this.git(dir, ["hash-object", "-w", "--stdin"], newGeneration).trim();
        const receiptOid = this.git(dir, ["hash-object", "-w", "--stdin"], content).trim();
        try {
          this.git(dir, ["update-ref", "--no-deref", "--stdin"], `start\nverify ${LAB_DOCUMENT_REF} ${head ?? ZERO}\nupdate ${LAB_GENERATION_REF} ${generationOid} ${current.generationOid ?? ZERO}\ncreate ${LAB_TRANSITIONS_PREFIX}${transitionId} ${receiptOid}\nprepare\ncommit\n`);
        } catch { throw new GitGenerationConflictError(); }
        return { receipt, head };
      });
    });
  }

  private async mapped(tenantId: string): Promise<{ dir: string; sourceRef: string }> {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(tenantId)) throw new Error("invalid tenant");
    const repo = await this.opts.locate(tenantId);
    await this.opts.authority.canonicalRemote(tenantId, repo.sourceRef);
    if (!existsSync(join(repo.dir, "HEAD"))) {
      await this.opts.authority.initialize(tenantId, repo.dir, repo.sourceRef);
    }
    if (this.git(repo.dir, ["rev-parse", "--is-bare-repository"]).trim() !== "true"
      || this.git(repo.dir, ["config", "--local", "--no-includes", "--get-all", "remote.origin.url"]).trim() !== repo.sourceRef) {
      throw new ForgePublicationError("refused", "Lab tenant repository mapping refused");
    }
    this.git(repo.dir, ["rev-parse", "--verify", "refs/heads/main^{commit}"]);
    return repo;
  }

  async commit(request: LabDocumentWrite): Promise<{ ref: typeof LAB_DOCUMENT_REF; commit: string }> {
    const { tenantId, expectedSha, message } = request;
    if (expectedSha !== null && (!SHA.test(expectedSha) || expectedSha === ZERO)) throw new Error("invalid expected SHA");
    if (!message.trim() || !request.documents.length) throw new Error("documents and message are required");
    const documents = request.documents.map(({ path, content }) => {
      if (!PATH.test(path)) throw new Error("Lab document path refused");
      const parsed: unknown = JSON.parse(content);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("Lab document must be a JSON object");
      return { path, content };
    });
    if (new Set(documents.map((doc) => doc.path)).size !== documents.length) throw new Error("duplicate document path");
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, async () => {
        const generation = await this.readGeneration(dir);
        if ((request.generation ?? null) !== generation.generation
          || (request.generation !== undefined && !validGeneration(request.generation))) throw new GitGenerationConflictError();
        const observed = this.readRef(dir);
        if (observed !== expectedSha) throw new GitRefConflictError(LAB_DOCUMENT_REF, expectedSha, observed);
        const blobs = new Map<string, string>();
        if (observed) {
          // Every ancestor must belong to the separate Lab history, never main.
          const separate = this.git(dir, ["rev-list", "--count", observed, "--not", "refs/heads/main"]).trim();
          if (separate !== this.git(dir, ["rev-list", "--count", observed]).trim()) {
            throw new Error("Lab history must be orphaned from main");
          }
          for (const entry of this.git(dir, ["ls-tree", "-rz", "--full-tree", observed]).split("\0").filter(Boolean)) {
            const match = /^100644 blob ([0-9a-f]{40})\t(.+)$/.exec(entry);
            if (!match || !PATH.test(match[2]!)) throw new Error("Lab tree contains a forbidden path or mode");
            blobs.set(match[2]!, match[1]!);
          }
        }
        for (const doc of documents) blobs.set(doc.path, this.git(dir, ["hash-object", "-w", "--stdin"], doc.content).trim());
        const tree = (prefix: string): string => {
          const entries = new Map<string, string>();
          for (const [path, sha] of blobs) {
            if (!path.startsWith(prefix)) continue;
            const tail = path.slice(prefix.length);
            const slash = tail.indexOf("/");
            if (slash === -1) entries.set(tail, `100644 blob ${sha}\t${tail}\0`);
            else {
              const name = tail.slice(0, slash);
              if (!entries.has(name)) entries.set(name, `040000 tree ${tree(`${prefix}${name}/`)}\t${name}\0`);
            }
          }
          return this.git(dir, ["mktree", "-z"], [...entries].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([, entry]) => entry).join("")).trim();
        };
        const commit = this.git(dir, ["commit-tree", tree(""), ...(observed ? ["-p", observed] : [])], message).trim();
        try { this.git(dir, ["update-ref", "--no-deref", "--stdin"], `start\nverify ${LAB_GENERATION_REF} ${generation.generationOid ?? ZERO}\nupdate ${LAB_DOCUMENT_REF} ${commit} ${expectedSha ?? ZERO}\nprepare\ncommit\n`); }
        catch (error) {
          const afterGeneration = await this.readGeneration(dir);
          const after = this.readRef(dir);
          if (afterGeneration.generationOid !== generation.generationOid) throw new GitGenerationConflictError();
          if (after !== expectedSha) throw new GitRefConflictError(LAB_DOCUMENT_REF, expectedSha, after);
          throw error;
        }
        return { ref: LAB_DOCUMENT_REF, commit };
      });
    });
  }

  async publishRef(tenantId: string, expectedSha: string | null, options?: { generation?: string; targetSha?: string }): Promise<{ commit: string }> {
    if ((options?.generation !== undefined && (typeof options.generation !== "string" || !validGeneration(options.generation)))
      || (options?.targetSha !== undefined && (typeof options.targetSha !== "string" || !SHA.test(options.targetSha)))) {
      throw new ForgePublicationError("refused", "invalid Lab publication options");
    }
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir, sourceRef } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, () =>
        this.opts.authority.publishRef(tenantId, dir, sourceRef, LAB_DOCUMENT_REF, expectedSha, options));
    });
  }
}
