import { execFile } from "node:child_process";
import { mkdtemp, mkdir, rm, realpath } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import type { AuthoritativePublishRequest, TenantBareRepo } from "../index.js";
import { GitRefConflictError } from "./tenantChangeRepo.js";

const MAIN = "refs/heads/main";
const SHA = /^[0-9a-f]{40}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const ZERO = "0".repeat(40);

export interface ForgeCredential {
  readonly tenantId: string;
  readonly remoteUrl: string;
  readonly username?: string;
  /** Scoped Forgejo access token. Omitted only for explicitly enabled local tests. */
  readonly token?: string;
}

export interface ForgeRemoteAuthorityOptions {
  /** Trusted tenant mapping, not a request-supplied URL. */
  locate(tenantId: string): Promise<string>;
  /** Called anew for every publish/retry/refresh. Null means revoked. */
  credentials(tenantId: string, remoteUrl: string): Promise<ForgeCredential | null>;
  allowLocalPathsForTests?: boolean;
  timeoutMs?: number;
}

export class ForgePublicationError extends Error {
  constructor(readonly state: "unknown" | "not-published" | "refused", message: string) {
    super(message);
    this.name = "ForgePublicationError";
  }
}

type Git = (args: string[], cwd?: string) => Promise<string>;

/** Remote truth for Mushy's validated artifact lifecycle. No deployment or CI. */
export class ForgeRemoteAuthority {
  private readonly timeoutMs: number;

  constructor(private readonly opts: ForgeRemoteAuthorityOptions) {
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 120_000) {
      throw new Error("invalid Forge Git timeout");
    }
  }

  private refuse(): never {
    throw new ForgePublicationError("refused", "Forge tenant, origin, credential or staged identity refused");
  }

  async canonicalRemote(tenantId: string, sourceRef: string): Promise<string> {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(tenantId)) this.refuse();
    let remote: string;
    try { remote = await this.opts.locate(tenantId); } catch { return this.refuse(); }
    if (remote !== sourceRef) this.refuse();
    if (this.opts.allowLocalPathsForTests && isAbsolute(remote)) return remote;
    try {
      const url = new URL(remote);
      if (url.protocol !== "https:" || !url.hostname || url.username || url.password
        || url.search || url.hash || url.href !== remote
        || !/^\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\.git$/.test(url.pathname)
        || url.pathname.split("/").some((part) => part === "." || part === "..")) this.refuse();
    } catch { this.refuse(); }
    return remote;
  }

  /** No shell, inherited credentials, global config, stderr or token-bearing argv. */
  protected execute(args: string[], cwd: string, env: NodeJS.ProcessEnv): Promise<string> {
    return new Promise((resolve, reject) => {
      execFile("git", args, {
        cwd, env, encoding: "utf8", timeout: this.timeoutMs,
        maxBuffer: 1024 * 1024, windowsHide: true,
      }, (error, stdout) => {
        if (error) reject(new ForgePublicationError("unknown", "Forge Git operation failed; remote state requires readback"));
        else resolve(stdout.trim());
      });
    });
  }

  private async session<T>(
    tenantId: string, dir: string, sourceRef: string,
    action: (git: Git, remote: string) => Promise<T>,
  ): Promise<T> {
    const remote = await this.canonicalRemote(tenantId, sourceRef);
    const scratch = await mkdtemp(join(tmpdir(), "leaf-forge-"));
    try {
      // Use an allowlist: GIT_*, proxies, tracing, askpass, helpers and arbitrary
      // provider variables cannot reach Git. HOME also cannot supply .netrc.
      const env: NodeJS.ProcessEnv = {};
      for (const [key, value] of Object.entries(process.env)) {
        if (/^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP)$/i.test(key)) env[key] = value;
      }
      Object.assign(env, {
        HOME: scratch, USERPROFILE: scratch, XDG_CONFIG_HOME: scratch,
        GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: join(scratch, "no-config"),
        GIT_TERMINAL_PROMPT: "0", GCM_INTERACTIVE: "never",
        GIT_NO_REPLACE_OBJECTS: "1", GIT_ALLOW_PROTOCOL: this.opts.allowLocalPathsForTests ? "https:file" : "https",
      });
      const safeDir = (await realpath(dir)).replaceAll("\\", "/");
      const cfg = ["-c", "safe.directory=", "-c", `safe.directory=${safeDir}`,
        "-c", `core.hooksPath=${join(scratch, "no-hooks")}`, "-c", "core.fsmonitor=false"];
      const local = (args: string[]) => this.execute([...cfg, ...args], dir, env);
      if (await local(["rev-parse", "--is-bare-repository"]) !== "true") this.refuse();
      // --no-includes reads the exact stored origin, without URL rewriting.
      const origin = await local(["config", "--local", "--no-includes", "--get-all", "remote.origin.url"]);
      if (origin !== remote) this.refuse();
      let credential: ForgeCredential | null;
      try { credential = await this.opts.credentials(tenantId, remote); } catch { return this.refuse(); }
      if (!credential || credential.tenantId !== tenantId || credential.remoteUrl !== remote) this.refuse();
      if ((!credential.token && !this.opts.allowLocalPathsForTests)
        || (credential.token !== undefined && !/^[A-Za-z0-9_-]+$/.test(credential.token))
        || (credential.username !== undefined && !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(credential.username))) this.refuse();
      // All network commands use a fresh bare transport, so cached local config
      // cannot rewrite URLs, install helpers or change the upload/receive program.
      await this.execute(["init", "--bare", "--template=", scratch], scratch, env);
      const networkEnv = { ...env, GIT_OBJECT_DIRECTORY: join(safeDir, "objects") };
      const config: Array<[string, string]> = [
        ["credential.helper", ""], ["http.followRedirects", "false"],
        ["http.sslVerify", "true"], ["http.extraHeader", ""],
      ];
      if (credential.token) config.push(["http.extraHeader",
        `Authorization: Basic ${Buffer.from(`${credential.username ?? "oauth2"}:${credential.token}`).toString("base64")}`]);
      Object.assign(networkEnv, { GIT_CONFIG_COUNT: String(config.length) });
      config.forEach(([key, value], i) => {
        (networkEnv as NodeJS.ProcessEnv)[`GIT_CONFIG_KEY_${i}`] = key;
        (networkEnv as NodeJS.ProcessEnv)[`GIT_CONFIG_VALUE_${i}`] = value;
      });
      return await action((args, cwd) => cwd === dir
        ? local(args)
        : this.execute([...cfg, ...args], scratch, networkEnv), remote);
    } finally {
      await rm(scratch, { recursive: true, force: true });
    }
  }

  private async head(git: Git, remote: string, ref: string): Promise<string | null> {
    const output = await git(["ls-remote", "--refs", remote, ref]);
    if (!output) return null;
    const lines = output.split(/\r?\n/);
    const [sha, name] = lines[0]!.split(/\s+/);
    if (lines.length !== 1 || name !== ref || !sha || !SHA.test(sha)) {
      throw new ForgePublicationError("unknown", "Forge remote readback is invalid");
    }
    return sha;
  }

  bind(tenantId: string, dir: string, sourceRef: string): TenantBareRepo {
    return {
      dir,
      publishAuthoritatively: (request) => this.publish(tenantId, dir, sourceRef, request),
      refreshMain: () => this.refresh(tenantId, dir, sourceRef),
    };
  }

  /** Bootstrap a cache without cloning through ambient Git transport config. */
  async initialize(tenantId: string, dir: string, sourceRef: string): Promise<void> {
    await this.canonicalRemote(tenantId, sourceRef);
    await mkdir(dir, { recursive: true });
    // Initialization has no network and no credential. The session verifies the
    // origin before its first fetch; the provider calls this only for a new dir.
    const env: NodeJS.ProcessEnv = {};
    for (const [key, value] of Object.entries(process.env)) {
      if (/^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP)$/i.test(key)) env[key] = value;
    }
    Object.assign(env, { GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null" });
    await this.execute(["init", "--bare", "--template=", "--initial-branch=main", dir], dir, env);
    await this.execute(["config", "--local", "remote.origin.url", sourceRef], dir, env);
    await this.refresh(tenantId, dir, sourceRef);
  }

  async refresh(tenantId: string, dir: string, sourceRef: string): Promise<void> {
    await this.session(tenantId, dir, sourceRef, async (git, remote) => {
      const observed = await this.head(git, remote, MAIN);
      if (!observed) throw new ForgePublicationError("unknown", "Forge main is missing");
      await git(["fetch", "--no-tags", "--no-write-fetch-head", remote, `${MAIN}:refs/leaf/observed`]);
      // Fetch can race a publisher. Never cache a different commit than observed.
      if (await git(["rev-parse", "refs/leaf/observed"]) !== observed) {
        throw new ForgePublicationError("unknown", "Forge main changed during refresh; retry");
      }
      const refs = await git(["for-each-ref", "--format=%(objectname)", MAIN], dir);
      if (refs && !SHA.test(refs)) this.refuse();
      if (refs === observed) return;
      if (refs) {
        try { await git(["merge-base", "--is-ancestor", refs, observed]); }
        catch { throw new GitRefConflictError(MAIN, refs, observed); }
      }
      await git(["update-ref", MAIN, observed, refs || ZERO], dir);
    });
  }

  async publish(
    tenantId: string, dir: string, sourceRef: string, request: AuthoritativePublishRequest,
  ): Promise<{ commit: string }> {
    const { receipt, expectedMainSha, stagedCommit, changeRef } = request;
    if (request.tenantId !== tenantId || receipt.tenant_id !== tenantId
      || receipt.contract !== "leaf.customization.v1" || receipt.state !== "staged"
      || !UUID.test(receipt.change_set_id) || !SHA.test(expectedMainSha)
      || !SHA.test(stagedCommit) || !SHA.test(receipt.base_commit)
      || receipt.staged_commit !== stagedCommit || receipt.base_commit !== expectedMainSha
      || changeRef !== `refs/leaf/changes/${receipt.change_set_id.toLowerCase()}`) this.refuse();
    return this.session(tenantId, dir, sourceRef, async (git, remote) => {
      const localChange = await git(["rev-parse", "--verify", changeRef], dir);
      if (localChange !== stagedCommit) this.refuse();
      if (await git(["cat-file", "-t", stagedCommit]) !== "commit") this.refuse();
      try { await git(["merge-base", "--is-ancestor", expectedMainSha, stagedCommit]); }
      catch { throw new GitRefConflictError(MAIN, expectedMainSha, stagedCommit); }
      const privateHead = await this.head(git, remote, changeRef);
      if (privateHead !== null && privateHead !== stagedCommit) {
        throw new GitRefConflictError(changeRef, stagedCommit, privateHead);
      }
      if (privateHead === null) {
        try {
          await git(["push", `--force-with-lease=${changeRef}:`, remote, `${stagedCommit}:${changeRef}`]);
        } catch {
          const after = await this.head(git, remote, changeRef);
          if (after !== stagedCommit) {
            if (after !== null) throw new GitRefConflictError(changeRef, stagedCommit, after);
            throw new ForgePublicationError("not-published", "Forge staged upload was not accepted");
          }
        }
      }
      const before = await this.head(git, remote, MAIN);
      if (before === stagedCommit) return { commit: stagedCommit };
      if (before !== expectedMainSha) throw new GitRefConflictError(MAIN, expectedMainSha, before);
      // Exact expected-old CAS is used only after proving fast-forward ancestry.
      // No '+' refspec, generic --force, worktree or branch tracking heuristic.
      try {
        await git(["push", `--force-with-lease=${MAIN}:${expectedMainSha}`, remote, `${stagedCommit}:${MAIN}`]);
      } catch {
        // A timeout may have happened after receive-pack committed. Readback is
        // the verdict even when push reports failure; read failure stays unknown.
      }
      const after = await this.head(git, remote, MAIN);
      if (after === stagedCommit) return { commit: stagedCommit };
      if (after === expectedMainSha) {
        throw new ForgePublicationError("not-published", "Forge main did not accept the staged commit");
      }
      throw new GitRefConflictError(MAIN, expectedMainSha, after);
    });
  }
}
