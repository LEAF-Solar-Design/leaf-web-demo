/** Isolated Glug author execution over the harness trust boundary. */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
} from "node:fs";
import { dirname, join, relative } from "node:path";
import { pathToFileURL } from "node:url";

import type { AgentGrant, OAuthGrantProvider } from "./ports/index.js";

const REQUEST_FIELDS = [
  "base_commit",
  "claim_id",
  "contract",
  "instruction",
  "power",
  "workspace",
] as const;
const AUTHOR_POWERS = new Set([
  "code_question",
  "announcement_draft",
  "schedule_draft",
  "stage_change",
]);
const READ_ONLY_POWERS = new Set([
  "code_question",
  "announcement_draft",
  "schedule_draft",
]);
const SHA40 = /^[0-9a-f]{40}$/;
const SAFE_ID = /^[a-z0-9][a-z0-9._-]{0,199}$/;
const PINNED_EDITOR_MODULE = "src/ports/impl/repoEditRunner.js";
const FIXED_COMMIT_SUBJECT = "chore(glug): stage Mushy maintenance proposal";
const AUTHOR_TIMEOUT_MS = 240 * 1000;
export const GLUG_MUSHY_SOURCE_COMMIT = "c3fdc0869692c804ae69fe00b5b6f0722c80943a";

export class GlugMushyAuthorError extends Error {
  constructor(
    readonly code: string,
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "GlugMushyAuthorError";
  }
}

export interface GlugMushyAuthorRequest {
  contract: "glug.mushy-author-request.v1";
  workspace: "glug";
  power: "code_question" | "announcement_draft" | "schedule_draft" | "stage_change";
  instruction: string;
  base_commit: string;
  claim_id: string;
}

export interface GlugMushyAuthor {
  run(
    input: Record<string, unknown>,
    sourceCommit: string,
    authorTimeoutSeconds: number,
  ): Promise<Record<string, unknown>>;
}

interface RepoEditorResult {
  summary: string;
}

interface RepoEditor {
  edit(input: {
    instruction: string;
    repoDir: string;
    grant: AgentGrant;
    context: string;
    signal: AbortSignal;
  }): Promise<RepoEditorResult>;
}

interface ArtifactFile {
  path: string;
  bytes: number;
  sha256: string;
}

interface AdoptionManifest {
  workspace_id: string;
  sources: { mushy_source_commit: string };
  artifact: {
    entrypoint: string;
    files: ArtifactFile[];
    byte_count: number;
    aggregate_sha256: string;
  };
  limits: { author_timeout_seconds: number };
}

export interface PinnedGlugMushyAuthorOptions {
  artifactRoot: string;
  manifestPath: string;
  workspaceRoot: string;
  sourceCommit: string;
  grantTenantId: string;
  grantProvider: OAuthGrantProvider;
  editorFactory?: (maxWallTimeMs: number) => Promise<RepoEditor> | RepoEditor;
}

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function authorTimeoutError(): GlugMushyAuthorError {
  return new GlugMushyAuthorError("author_timeout", 504, "Glug Mushy author timed out");
}

function remainingAuthorTime(deadlineMs: number, signal: AbortSignal): number {
  const remaining = deadlineMs - Date.now();
  if (signal.aborted || remaining <= 0) throw authorTimeoutError();
  return remaining;
}

async function awaitBeforeAuthorDeadline<T>(
  operation: Promise<T>,
  signal: AbortSignal,
): Promise<T> {
  if (signal.aborted) throw authorTimeoutError();
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(authorTimeoutError());
    signal.addEventListener("abort", onAbort, { once: true });
    operation.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        if (signal.aborted) reject(authorTimeoutError());
        else resolve(value);
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort);
        reject(signal.aborted ? authorTimeoutError() : error);
      },
    );
  });
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  return actual.length === wanted.length && actual.every((item, index) => item === wanted[index]);
}

function safeArtifactPath(value: unknown): value is string {
  if (typeof value !== "string" || !value || value.includes("\\") || value.startsWith("/")) {
    return false;
  }
  const parts = value.split("/");
  return parts.every((part) => part && part !== "." && part !== "..");
}

function artifactFiles(root: string): string[] {
  const found: string[] = [];
  const visit = (directory: string): void => {
    for (const name of readdirSync(directory).sort()) {
      const target = join(directory, name);
      const stat = lstatSync(target);
      if (stat.isSymbolicLink()) {
        throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact contains a link");
      }
      if (stat.isDirectory()) visit(target);
      else if (stat.isFile()) found.push(relative(root, target).replaceAll("\\", "/"));
      else throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact contains a special file");
    }
  };
  visit(root);
  return found.sort();
}

/** Verify the exact artifact tree from the image-pinned adoption manifest. */
export function verifyPinnedMushyArtifact(
  artifactRoot: string,
  manifestPath: string,
  expectedSourceCommit: string,
): AdoptionManifest {
  const rootStat = lstatSync(artifactRoot);
  const manifestStat = lstatSync(manifestPath);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink() || !manifestStat.isFile() || manifestStat.isSymbolicLink()) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact is unavailable");
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as AdoptionManifest;
  if (
    manifest.workspace_id !== "glug"
    || manifest.sources?.mushy_source_commit !== expectedSourceCommit
    || !SHA40.test(expectedSourceCommit)
    || !manifest.artifact
    || !Array.isArray(manifest.artifact.files)
    || manifest.limits?.author_timeout_seconds !== 240
  ) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact manifest is invalid");
  }
  const declared = manifest.artifact.files;
  if (!declared.length || !safeArtifactPath(manifest.artifact.entrypoint)) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact manifest is invalid");
  }
  const paths = declared.map((entry) => entry.path);
  if (
    paths.some((path) => !safeArtifactPath(path))
    || paths.some((path, index) => index > 0 && path <= paths[index - 1]!)
    || !paths.includes(manifest.artifact.entrypoint)
    || !paths.includes(PINNED_EDITOR_MODULE)
  ) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact file list is invalid");
  }
  if (artifactFiles(artifactRoot).join("\0") !== paths.join("\0")) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact tree drifted");
  }
  let byteCount = 0;
  for (const entry of declared) {
    const bytes = readFileSync(join(artifactRoot, entry.path));
    if (
      !Number.isSafeInteger(entry.bytes)
      || entry.bytes < 0
      || !/^[0-9a-f]{64}$/.test(entry.sha256)
      || bytes.length !== entry.bytes
      || sha256(bytes) !== entry.sha256
    ) {
      throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact digest drifted");
    }
    byteCount += bytes.length;
  }
  const canonicalFiles = declared.map((entry) => ({
    bytes: entry.bytes,
    path: entry.path,
    sha256: entry.sha256,
  }));
  if (
    byteCount !== manifest.artifact.byte_count
    || sha256(JSON.stringify(canonicalFiles)) !== manifest.artifact.aggregate_sha256
  ) {
    throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned artifact aggregate drifted");
  }
  return manifest;
}

function git(repoDir: string, ...args: string[]): string {
  return execFileSync("git", ["-C", repoDir, ...args], {
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

function ignoredFiles(repoDir: string): string[] {
  const raw = execFileSync(
    "git",
    ["-C", repoDir, "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
    { encoding: "utf8", maxBuffer: 16 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] },
  );
  return raw.split("\0").filter(Boolean);
}

function assertStandaloneCleanRepository(repoDir: string, baseCommit: string): void {
  const gitDir = join(repoDir, ".git");
  const repoStat = lstatSync(repoDir);
  const gitStat = lstatSync(gitDir);
  if (
    !repoStat.isDirectory()
    || repoStat.isSymbolicLink()
    || !gitStat.isDirectory()
    || gitStat.isSymbolicLink()
  ) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "standalone workspace is required");
  }
  if (realpathSync(git(repoDir, "rev-parse", "--show-toplevel")) !== realpathSync(repoDir)) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace root is invalid");
  }
  if (git(repoDir, "rev-parse", "HEAD") !== baseCommit) {
    throw new GlugMushyAuthorError("base_drift", 409, "workspace base drifted");
  }
  if (
    git(repoDir, "status", "--porcelain=v1", "--untracked-files=all")
    || ignoredFiles(repoDir).length
    || git(repoDir, "replace", "-l")
  ) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace is not clean");
  }
  const index = git(repoDir, "ls-files", "--stage").split("\n").filter(Boolean);
  if (index.some((line) => line.startsWith("120000 ") || line.startsWith("160000 "))) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace links are unavailable");
  }
}

function assertUnchangedRepositoryAuthority(repoDir: string, baseCommit: string): void {
  if (
    git(repoDir, "rev-parse", "HEAD") !== baseCommit
    || git(repoDir, "replace", "-l")
  ) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace authority changed");
  }
}

function assertIndexHasNoLinks(repoDir: string): void {
  const index = git(repoDir, "ls-files", "--stage").split("\n").filter(Boolean);
  if (index.some((line) => line.startsWith("120000 ") || line.startsWith("160000 "))) {
    throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace links are unavailable");
  }
}

function restoreReadOnly(repoDir: string, baseCommit: string): void {
  git(repoDir, "reset", "--hard", baseCommit);
  git(repoDir, "clean", "-ffdx");
  try {
    assertStandaloneCleanRepository(repoDir, baseCommit);
  } catch {
    throw new GlugMushyAuthorError(
      "read_only_cleanup_failed",
      503,
      "read-only cleanup failed",
    );
  }
}

function parseRequest(value: Record<string, unknown>): GlugMushyAuthorRequest {
  if (!exactKeys(value, REQUEST_FIELDS)) {
    throw new GlugMushyAuthorError("request_invalid", 422, "author request fields are invalid");
  }
  const power = value.power;
  if (
    value.contract !== "glug.mushy-author-request.v1"
    || value.workspace !== "glug"
    || typeof power !== "string"
    || !AUTHOR_POWERS.has(power)
    || typeof value.instruction !== "string"
    || !value.instruction.trim()
    || value.instruction.length > 20_000
    || typeof value.base_commit !== "string"
    || !SHA40.test(value.base_commit)
    || typeof value.claim_id !== "string"
    || !SAFE_ID.test(value.claim_id)
  ) {
    throw new GlugMushyAuthorError("request_invalid", 422, "author request is invalid");
  }
  return value as unknown as GlugMushyAuthorRequest;
}

export class PinnedGlugMushyAuthor implements GlugMushyAuthor {
  private readonly artifactRoot: string;
  private readonly workspaceRoot: string;
  private readonly manifestPath: string;

  constructor(private readonly opts: PinnedGlugMushyAuthorOptions) {
    verifyPinnedMushyArtifact(opts.artifactRoot, opts.manifestPath, opts.sourceCommit);
    const workspaceStat = lstatSync(opts.workspaceRoot);
    if (!workspaceStat.isDirectory() || workspaceStat.isSymbolicLink()) {
      throw new GlugMushyAuthorError("workspace_invalid", 503, "workspace root is unavailable");
    }
    this.artifactRoot = realpathSync(opts.artifactRoot);
    this.workspaceRoot = realpathSync(opts.workspaceRoot);
    this.manifestPath = realpathSync(opts.manifestPath);
  }

  async run(
    value: Record<string, unknown>,
    sourceCommit: string,
    authorTimeoutSeconds: number,
  ): Promise<Record<string, unknown>> {
    const controller = new AbortController();
    const deadlineMs = Date.now() + AUTHOR_TIMEOUT_MS;
    const timer = setTimeout(() => controller.abort(), AUTHOR_TIMEOUT_MS);
    timer.unref?.();
    try {
      const input = parseRequest(value);
      const manifest = verifyPinnedMushyArtifact(
        this.artifactRoot,
        this.manifestPath,
        this.opts.sourceCommit,
      );
      if (
        sourceCommit !== this.opts.sourceCommit
        || authorTimeoutSeconds !== manifest.limits.author_timeout_seconds
      ) {
        throw new GlugMushyAuthorError("pin_invalid", 409, "author pin does not match");
      }
      remainingAuthorTime(deadlineMs, controller.signal);
      const container = join(this.workspaceRoot, sha256(input.claim_id));
      const repoDir = join(container, "repository");
      const containerStat = lstatSync(container);
      const repoStat = lstatSync(repoDir);
      if (
        !containerStat.isDirectory()
        || containerStat.isSymbolicLink()
        || !repoStat.isDirectory()
        || repoStat.isSymbolicLink()
      ) {
        throw new GlugMushyAuthorError("workspace_invalid", 409, "standalone workspace is required");
      }
      const resolvedContainer = realpathSync(container);
      const resolvedRepo = realpathSync(repoDir);
      if (dirname(resolvedContainer) !== this.workspaceRoot || dirname(resolvedRepo) !== resolvedContainer) {
        throw new GlugMushyAuthorError("workspace_invalid", 409, "workspace escaped its root");
      }
      assertStandaloneCleanRepository(resolvedRepo, input.base_commit);
      remainingAuthorTime(deadlineMs, controller.signal);

      const grant = await awaitBeforeAuthorDeadline(
        this.opts.grantProvider.getGrant(this.opts.grantTenantId),
        controller.signal,
      );
      const editor = this.opts.editorFactory
        ? await awaitBeforeAuthorDeadline(
            Promise.resolve(this.opts.editorFactory(
              remainingAuthorTime(deadlineMs, controller.signal),
            )),
            controller.signal,
          )
        : await this.loadEditor(deadlineMs, controller.signal);
      const context = READ_ONLY_POWERS.has(input.power)
        ? `Glug ${input.power} is read-only. Do not edit or create files. Return the requested answer or draft in the final summary.`
        : "Stage one bounded Glug-only source change. Do not run Git, publish, push, merge, deploy, or access provider systems. The trusted shell commits the resulting files.";

      if (READ_ONLY_POWERS.has(input.power)) {
        let result: RepoEditorResult | undefined;
        try {
          result = await editor.edit({
            instruction: input.instruction,
            repoDir: resolvedRepo,
            grant,
            context,
            signal: controller.signal,
          });
          remainingAuthorTime(deadlineMs, controller.signal);
        } catch (error) {
          if (controller.signal.aborted || Date.now() >= deadlineMs) throw authorTimeoutError();
          throw error;
        } finally {
          restoreReadOnly(resolvedRepo, input.base_commit);
        }
        if (
          !result
          || typeof result.summary !== "string"
          || !result.summary.trim()
          || Buffer.byteLength(result.summary, "utf8") > 48 * 1024
        ) {
          throw new GlugMushyAuthorError("result_invalid", 502, "read-only author returned no answer");
        }
        return { text: result.summary.trim() };
      }

      try {
        await editor.edit({
          instruction: input.instruction,
          repoDir: resolvedRepo,
          grant,
          context,
          signal: controller.signal,
        });
        remainingAuthorTime(deadlineMs, controller.signal);
        assertUnchangedRepositoryAuthority(resolvedRepo, input.base_commit);
        if (ignoredFiles(resolvedRepo).length) {
          throw new GlugMushyAuthorError("result_invalid", 409, "author left ignored files");
        }
        if (!git(resolvedRepo, "status", "--porcelain=v1", "--untracked-files=all")) {
          throw new GlugMushyAuthorError("result_invalid", 409, "author produced no source change");
        }
        remainingAuthorTime(deadlineMs, controller.signal);
        git(resolvedRepo, "add", "-A");
        assertIndexHasNoLinks(resolvedRepo);
        remainingAuthorTime(deadlineMs, controller.signal);
        execFileSync(
          "git",
          [
            "-C", resolvedRepo,
            "-c", "user.name=Glug Mushy",
            "-c", "user.email=mushy@glug.invalid",
            "-c", "core.hooksPath=",
            "commit", "-m", FIXED_COMMIT_SUBJECT,
            "-m", `Mushy-Source: ${this.opts.sourceCommit}`,
          ],
          { encoding: "utf8", maxBuffer: 16 * 1024 * 1024, stdio: ["ignore", "pipe", "pipe"] },
        );
        remainingAuthorTime(deadlineMs, controller.signal);
        if (git(resolvedRepo, "status", "--porcelain=v1", "--untracked-files=all") || ignoredFiles(resolvedRepo).length) {
          throw new GlugMushyAuthorError("result_invalid", 409, "author result is not clean");
        }
        return {};
      } catch (error) {
        if (controller.signal.aborted || Date.now() >= deadlineMs) {
          restoreReadOnly(resolvedRepo, input.base_commit);
          throw authorTimeoutError();
        }
        throw error;
      }
    } finally {
      clearTimeout(timer);
    }
  }

  private async loadEditor(deadlineMs: number, signal: AbortSignal): Promise<RepoEditor> {
    const modulePath = join(this.artifactRoot, PINNED_EDITOR_MODULE);
    const loaded = await awaitBeforeAuthorDeadline(
      import(pathToFileURL(modulePath).href),
      signal,
    ) as { SdkRepoEditor?: new (options: { maxTurns: number; maxWallTimeMs: number }) => RepoEditor };
    if (typeof loaded.SdkRepoEditor !== "function") {
      throw new GlugMushyAuthorError("artifact_invalid", 503, "pinned editor export is unavailable");
    }
    return new loaded.SdkRepoEditor({
      maxTurns: 40,
      maxWallTimeMs: remainingAuthorTime(deadlineMs, signal),
    });
  }
}
