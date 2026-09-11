/** Forgejo publication supplier for the Mushy platform's project repositories. */
import { execFile } from "node:child_process";
import { readFileSync, statSync } from "node:fs";
import { mkdtemp, readFile, rm, realpath } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join } from "node:path";
import type { ProjectRepositoryAuthority } from "../index.js";
import type { TenantChangeSet } from "../../vendor/mushy-author/ports/impl/tenantChangeRepo.js";
import { ProjectRepositoryEditCoordinator } from "../../agent/projectRepositoryEditCoordinator.js";
import type { ProjectRepositoryEditCoordinatorPorts } from "../../agent/projectRepositoryEditCoordinator.js";
import { ProjectRepositoryEditCoordinationClient } from "./projectRepositoryEditCoordinationClient.js";

const MAIN = "refs/heads/main";
const SHA = /^[0-9a-f]{40}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const KEYS = ["organizationId", "projectId", "repoKey", "tenantId"] as const;
const ZERO = "0".repeat(40);

export class ProjectForgePublicationError extends Error {
  constructor(readonly state: "unknown" | "not-published" | "refused") {
    super(`project Forge publication ${state}`);
    this.name = "ProjectForgePublicationError";
  }
}
function refuse(): never { throw new ProjectForgePublicationError("refused"); }
function authorityCopy(value: unknown): ProjectRepositoryAuthority {
  if (!value || typeof value !== "object" || Array.isArray(value)) refuse();
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join(",") !== KEYS.join(",")) refuse();
  for (const key of KEYS) if (typeof record[key] !== "string" || !UUID.test(record[key])) refuse();
  return Object.freeze({ tenantId: record.tenantId as string, organizationId: record.organizationId as string,
    projectId: record.projectId as string, repoKey: record.repoKey as string });
}
function identity(value: ProjectRepositoryAuthority): string {
  return KEYS.map(key => value[key]).join(":");
}
function canonicalRemote(value: unknown, local: boolean): string {
  if (typeof value !== "string") refuse();
  if (local && isAbsolute(value) && !value.includes("\0")) return value;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password ||
        url.search || url.hash || url.href !== value ||
        !/^\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+\.git$/.test(url.pathname) ||
        url.pathname.split("/").some(part => part === "." || part === "..")) refuse();
  } catch { refuse(); }
  return value;
}
export interface ProjectForgeCredential {
  readonly authority: ProjectRepositoryAuthority;
  readonly remoteUrl: string;
  readonly username?: string;
  readonly token?: string;
}
export interface ProjectForgeAuthorityOptions {
  locate(authority: ProjectRepositoryAuthority): Promise<string>;
  credentials(authority: ProjectRepositoryAuthority, remote: string): Promise<ProjectForgeCredential | null>;
  allowLocalPathsForTests?: boolean;
  timeoutMs?: number;
}
type Git = (args: string[], local?: boolean) => Promise<string>;

export class ProjectForgeAuthority {
  private readonly timeoutMs: number;
  private readonly boundRemotes = new Map<string, string>();
  constructor(private readonly opts: ProjectForgeAuthorityOptions) {
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    if (!Number.isFinite(this.timeoutMs) || this.timeoutMs < 1 || this.timeoutMs > 120_000) refuse();
  }

  protected execute(args: string[], cwd: string, env: NodeJS.ProcessEnv): Promise<string> {
    return new Promise((resolve, reject) => {
      execFile("git", args, { cwd, env, encoding: "utf8", timeout: this.timeoutMs,
        maxBuffer: 1024 * 1024, windowsHide: true }, (error, stdout) => {
        if (error) reject(new ProjectForgePublicationError("unknown"));
        else resolve(stdout.trim());
      });
    });
  }

  private async session<T>(authority: ProjectRepositoryAuthority, dir: string,
    action: (git: Git, remote: string) => Promise<T>): Promise<T> {
    let scratch: string | undefined;
    try {
      let remote: string;
      let credential: ProjectForgeCredential | null;
      try {
        remote = canonicalRemote(await this.opts.locate(authority), !!this.opts.allowLocalPathsForTests);
        const key = identity(authority);
        const previous = this.boundRemotes.get(key);
        if (previous !== undefined && previous !== remote) refuse();
        this.boundRemotes.set(key, remote);
        credential = await this.opts.credentials(authority, remote);
        if (!credential || identity(authorityCopy(credential.authority)) !== identity(authority) ||
            credential.remoteUrl !== remote ||
            (!credential.token && !(this.opts.allowLocalPathsForTests && isAbsolute(remote))) ||
            (credential.token !== undefined && (typeof credential.token !== "string" || !/^[A-Za-z0-9_-]+$/.test(credential.token))) ||
            (credential.username !== undefined && (typeof credential.username !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(credential.username)))) refuse();
        credential = Object.freeze({ authority, remoteUrl: remote,
          ...(credential.token !== undefined ? { token: credential.token } : {}),
          ...(credential.username !== undefined ? { username: credential.username } : {}) });
      } catch { return refuse(); }
      scratch = await mkdtemp(join(tmpdir(), "leaf-project-forge-"));
      const env: NodeJS.ProcessEnv = {};
      for (const [key, value] of Object.entries(process.env)) {
        if (/^(PATH|SYSTEMROOT|WINDIR|TEMP|TMP)$/i.test(key)) env[key] = value;
      }
      Object.assign(env, {
        HOME: scratch, USERPROFILE: scratch, XDG_CONFIG_HOME: scratch,
        GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: join(scratch, "no-config"),
        GIT_TERMINAL_PROMPT: "0", GCM_INTERACTIVE: "never", GIT_NO_REPLACE_OBJECTS: "1",
        GIT_ALLOW_PROTOCOL: this.opts.allowLocalPathsForTests ? "https:file" : "https",
      });
      const safeDir = (await realpath(dir)).replaceAll("\\", "/");
      const cacheConfig = await readFile(join(safeDir, "config"), "utf8");
      for (const line of cacheConfig.split(/\r?\n/).map(value => value.trim()).filter(Boolean)) {
        if (line !== "[core]" && !/^(repositoryformatversion\s*=\s*0|filemode\s*=\s*(true|false)|bare\s*=\s*true|logallrefupdates\s*=\s*(true|false)|ignorecase\s*=\s*(true|false)|symlinks\s*=\s*(true|false))$/.test(line)) refuse();
      }
      for (const path of ["objects/info/alternates", "objects/info/http-alternates", "info/grafts"]) {
        try { await readFile(join(safeDir, path)); }
        catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") continue;
          return refuse();
        }
        refuse();
      }
      const cfg = ["-c", "safe.directory=", "-c", `safe.directory=${safeDir}`,
        "-c", `core.hooksPath=${join(scratch, "no-hooks")}`, "-c", "core.fsmonitor=false"];
      const local = (args: string[]) => this.execute([...cfg, ...args], dir, env);
      if (await local(["rev-parse", "--is-bare-repository"]) !== "true") refuse();
      // The project provider validates ownership. No origin is required or written.
      await this.execute(["init", "--bare", "--template=", scratch], scratch, env);
      const networkEnv: NodeJS.ProcessEnv = { ...env, GIT_OBJECT_DIRECTORY: join(safeDir, "objects") };
      const config: Array<[string, string]> = [
        ["credential.helper", ""], ["http.followRedirects", "false"],
        ["http.sslVerify", "true"], ["http.extraHeader", ""],
      ];
      if (credential.token) config.push(["http.extraHeader",
        `Authorization: Basic ${Buffer.from(`${credential.username ?? "oauth2"}:${credential.token}`).toString("base64")}`]);
      networkEnv.GIT_CONFIG_COUNT = String(config.length);
      config.forEach(([key, value], i) => {
        networkEnv[`GIT_CONFIG_KEY_${i}`] = key;
        networkEnv[`GIT_CONFIG_VALUE_${i}`] = value;
      });
      const transportDir = scratch;
      return await action((args, inCache) => inCache ? local(args)
        : this.execute([...cfg, ...args], transportDir, networkEnv), remote);
    } catch (error) {
      // Discard supplier, filesystem and child-process details at the public boundary.
      throw new ProjectForgePublicationError(error instanceof ProjectForgePublicationError ? error.state : "unknown");
    } finally {
      if (scratch) await rm(scratch, { recursive: true, force: true }).catch(() => {});
    }
  }

  private async head(git: Git, remote: string, ref: string): Promise<string | null> {
    const output = await git(["ls-remote", "--refs", remote, ref]);
    if (!output) return null;
    const lines = output.split(/\r?\n/);
    const [sha, name] = lines[0]!.split(/\s+/);
    if (lines.length !== 1 || name !== ref || !sha || !SHA.test(sha)) {
      throw new ProjectForgePublicationError("unknown");
    }
    return sha;
  }

  bind(value: ProjectRepositoryAuthority, repoDir: string): {
    refreshMain(): Promise<void>;
    publishAuthoritatively(change: TenantChangeSet, expectedMainSha: string): Promise<{ commit: string }>;
  } {
    const authority = authorityCopy(value);
    return {
      refreshMain: () => this.refresh(authority, repoDir),
      publishAuthoritatively: (change, expected) => this.publish(authority, repoDir, change, expected),
    };
  }

  private async refresh(authority: ProjectRepositoryAuthority, dir: string): Promise<void> {
    await this.session(authority, dir, async (git, remote) => {
      const observed = await this.head(git, remote, MAIN);
      if (!observed) throw new ProjectForgePublicationError("unknown");
      await git(["fetch", "--no-tags", "--no-write-fetch-head", remote, `${MAIN}:refs/leaf/observed`]);
      if (await git(["rev-parse", "refs/leaf/observed"]) !== observed) {
        throw new ProjectForgePublicationError("unknown");
      }
      const current = await git(["for-each-ref", "--format=%(objectname)", MAIN], true);
      if (current && !SHA.test(current)) refuse();
      if (current === observed) return;
      if (current) await git(["merge-base", "--is-ancestor", current, observed]);
      await git(["update-ref", MAIN, observed, current || ZERO], true);
    });
  }

  private async publish(authority: ProjectRepositoryAuthority, dir: string,
    change: TenantChangeSet, expected: string): Promise<{ commit: string }> {
    const { id, ref, stagedSha: staged, expectedBaseSha: base } = change;
    if (!UUID.test(id) || ref !== `refs/leaf/changes/${id}` || !SHA.test(expected) ||
        !staged || !SHA.test(staged) || !SHA.test(base) || base !== expected) refuse();
    return this.session(authority, dir, async (git, remote) => {
      if (await git(["rev-parse", "--verify", ref], true) !== staged ||
          await git(["cat-file", "-t", staged]) !== "commit") refuse();
      await git(["merge-base", "--is-ancestor", expected, staged]);
      const privateHead = await this.head(git, remote, ref);
      if (privateHead !== null && privateHead !== staged) refuse();
      if (privateHead === null) {
        try { await git(["push", `--force-with-lease=${ref}:`, remote, `${staged}:${ref}`]); }
        catch {
          if (await this.head(git, remote, ref) !== staged) throw new ProjectForgePublicationError("not-published");
        }
      }
      if (await this.head(git, remote, ref) !== staged) throw new ProjectForgePublicationError("unknown");
      const before = await this.head(git, remote, MAIN);
      if (before === staged) return { commit: staged };
      if (before !== expected) throw new ProjectForgePublicationError("not-published");
      try { await git(["push", `--force-with-lease=${MAIN}:${expected}`, remote, `${staged}:${MAIN}`]); }
      catch { /* A lost response requires exact readback, never a local cache update. */ }
      const after = await this.head(git, remote, MAIN);
      if (after === staged) return { commit: staged };
      throw new ProjectForgePublicationError("not-published");
    });
  }
}

/** Load trusted topology once; read the current credential for each operation. */
export function createProjectForgeAuthorityFromEnv(env: NodeJS.ProcessEnv = process.env): ProjectForgeAuthority | undefined {
  const mapPath = env.LEAF_FORGE_ORIGIN_MAP;
  const credentialDir = env.LEAF_FORGE_CREDENTIAL_DIR;
  if (mapPath === undefined && credentialDir === undefined) return undefined;
  try {
    if (!mapPath || !credentialDir || !isAbsolute(mapPath) || !isAbsolute(credentialDir) ||
        !statSync(credentialDir).isDirectory()) refuse();
    const map: unknown = JSON.parse(readFileSync(mapPath, "utf8"));
    if (!map || typeof map !== "object" || Array.isArray(map)) refuse();
    const record = map as Record<string, unknown>;
    if (Object.keys(record).sort().join(",") !== "repositories,version" || record.version !== 1 ||
        !Array.isArray(record.repositories)) refuse();
    const origins = new Map<string, string>();
    const repoKeys = new Set<string>();
    const remotes = new Set<string>();
    for (const entry of record.repositories) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry) ||
          Object.keys(entry).sort().join(",") !== "authority,remoteUrl") refuse();
      const authority = authorityCopy(entry.authority);
      const remote = canonicalRemote(entry.remoteUrl, false);
      if (new URL(remote).origin !== "https://forge.leafdesign.ai" || origins.has(identity(authority)) ||
          repoKeys.has(authority.repoKey) || remotes.has(remote)) refuse();
      origins.set(identity(authority), remote);
      repoKeys.add(authority.repoKey);
      remotes.add(remote);
    }
    return new ProjectForgeAuthority({
      async locate(authority) { return origins.get(identity(authority)) ?? refuse(); },
      async credentials(authority, remoteUrl) {
        let contents: string;
        try { contents = await readFile(join(credentialDir, `${authority.repoKey}.json`), "utf8"); }
        catch (error) {
          if ((error as NodeJS.ErrnoException).code === "ENOENT") return null;
          return refuse();
        }
        const value: unknown = JSON.parse(contents);
        if (!value || typeof value !== "object" || Array.isArray(value)) refuse();
        const record = value as Record<string, unknown>;
        if (Object.keys(record).sort().join(",") !== (record.username === undefined
          ? "authority,remoteUrl,token" : "authority,remoteUrl,token,username")) refuse();
        const bound = authorityCopy(record.authority);
        if (identity(bound) !== identity(authority) || record.remoteUrl !== remoteUrl ||
            typeof record.token !== "string" || !/^[A-Za-z0-9_-]+$/.test(record.token) ||
            (record.username !== undefined && typeof record.username !== "string")) refuse();
        return { authority: bound, remoteUrl, token: record.token,
          ...(typeof record.username === "string" ? { username: record.username } : {}) };
      },
    });
  } catch { throw new ProjectForgePublicationError("refused"); }
}

/** Shared by the live serve entry and its runtime composition fixture. */
export function createProjectRepositoryEditsForService(
  provider: ProjectRepositoryEditCoordinatorPorts["leases"] & {
    projectChangeRepo: ProjectRepositoryEditCoordinatorPorts["changeRepo"];
  },
  env: NodeJS.ProcessEnv = process.env,
): ProjectRepositoryEditCoordinator | undefined {
  const appUrl = (env.LEAF_APP_URL ?? "").trim();
  const dispatchSecret = (env.LEAF_APP_DISPATCH_SECRET ?? "").trim();
  if (!appUrl || !dispatchSecret) return undefined;
  return new ProjectRepositoryEditCoordinator({
    leases: provider, changeRepo: authority => provider.projectChangeRepo(authority),
    coordination: new ProjectRepositoryEditCoordinationClient({ baseUrl: appUrl, dispatchSecret }),
  });
}
