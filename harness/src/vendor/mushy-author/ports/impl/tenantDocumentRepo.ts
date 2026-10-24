import { execFileSync } from "node:child_process";
import { realpathSync } from "node:fs";
import type { HarnessIdentity } from "../index.js";
import { LAB_DOCUMENT_REF } from "../labDocumentPort.js";
import type { LabDocumentPort, LabDocumentWrite } from "../labDocumentPort.js";
import type { PgTenantRepoLeaseCoordinator } from "./tenantRepoProvider.js";
import { ForgePublicationError } from "./forgeRemoteAuthority.js";
import type { ForgeRemoteAuthority } from "./forgeRemoteAuthority.js";
import { GitRefConflictError } from "./tenantChangeRepo.js";

const SHA = /^[0-9a-f]{40}$(?![\s\S])/;
const PATH = /^lab\/(graphs|widgets)\/[A-Za-z0-9][A-Za-z0-9_-]*\.json$(?![\s\S])/;
const ZERO = "0".repeat(40);

export interface TenantDocumentRepoOptions {
  /** Server-owned mapping only. No repository path or URL comes from a write request. */
  locate(tenantId: string): Promise<{ dir: string; sourceRef: string }>;
  lease: Pick<PgTenantRepoLeaseCoordinator, "withLease" | "runFenced">;
  authority: ForgeRemoteAuthority;
  identity: HarnessIdentity;
}

/** No checkout, index, filters, hooks or worktree. Only Git objects and the Lab ref. */
export class TenantDocumentRepo implements LabDocumentPort {
  constructor(private readonly opts: TenantDocumentRepoOptions) {}

  private git(dir: string, args: string[], input?: string): string {
    const env: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (/^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP)$/i.test(key)) env[key] = value;
    }
    Object.assign(env, {
      GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null",
      GIT_NO_REPLACE_OBJECTS: "1", GIT_TERMINAL_PROMPT: "0",
    });
    try {
      return execFileSync("git", [
        "-c", "safe.directory=", "-c", `safe.directory=${realpathSync(dir).replaceAll("\\", "/")}`,
        "-c", `core.hooksPath=${process.platform === "win32" ? "NUL" : "/dev/null"}`,
        "-c", "core.fsmonitor=false",
        "-c", "commit.gpgSign=false", "-c", `user.name=${this.opts.identity.name}`,
        "-c", `user.email=${this.opts.identity.email}`, ...args,
      ], { cwd: dir, env, input, encoding: "utf8", windowsHide: true });
    } catch {
      throw new Error("Lab Git operation failed");
    }
  }

  private readRef(dir: string): string | null {
    const value = this.git(dir, ["for-each-ref", "--format=%(objectname)%09%(symref)", LAB_DOCUMENT_REF]).trim();
    if (value && !SHA.test(value)) throw new Error("invalid Lab ref");
    return value || null;
  }

  private async mapped(tenantId: string): Promise<{ dir: string; sourceRef: string }> {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(tenantId)) throw new Error("invalid tenant");
    const repo = await this.opts.locate(tenantId);
    await this.opts.authority.canonicalRemote(tenantId, repo.sourceRef);
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
        try { this.git(dir, ["update-ref", "--no-deref", LAB_DOCUMENT_REF, commit, expectedSha ?? ZERO]); }
        catch (error) {
          const after = this.readRef(dir);
          if (after !== expectedSha) throw new GitRefConflictError(LAB_DOCUMENT_REF, expectedSha, after);
          throw error;
        }
        return { ref: LAB_DOCUMENT_REF, commit };
      });
    });
  }

  async publishRef(tenantId: string, expectedSha: string | null): Promise<{ commit: string }> {
    return this.opts.lease.withLease(tenantId, async (lease) => {
      const { dir, sourceRef } = await this.mapped(tenantId);
      return this.opts.lease.runFenced(lease, () =>
        this.opts.authority.publishRef(tenantId, dir, sourceRef, LAB_DOCUMENT_REF, expectedSha));
    });
  }
}
