/**
 * Free-form in-app repo editor — the "do whatever I want" author surface.
 *
 * The mushy thesis, stated plainly: an application is a thin immutable shell
 * plus a git repo of soft substance (UI files, capability tools, registry),
 * with Claude BUNDLED IN-APP as the editor of that repo. This runner is the
 * bundle's edit loop: one SDK session per instruction, granted read / write /
 * list / delete over the CONSUMER'S REPO ONLY (containment-checked, .git
 * protected), returning the changed file set for the caller to validate and
 * commit. It deliberately has no three-tool straitjacket: the hardened
 * structured author loop (agentSdkRunner + submitToolProposal) remains the
 * right boundary for multi-tenant SaaS; THIS runner is the right boundary for
 * a single-operator app editing itself.
 *
 * Same non-negotiables as every runner here: env scrubbed, the grant injected
 * explicitly (subscription OAuth or BYO key), never logged; the caller owns
 * validation-before-commit and the commit itself.
 */

import { execFileSync } from "node:child_process";
import {
  closeSync,
  ftruncateSync,
  fstatSync,
  lstatSync,
  mkdtempSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  readdirSync,
  renameSync,
  rmSync,
  rmdirSync,
  writeSync,
} from "node:fs";
import { basename, dirname, join } from "node:path";
import type { AgentGrant, AuthorTelemetry } from "../index.js";
import {
  assertOpenedFileIdentity,
  isWindowsUnsafeRepoComponent,
  resolveContainedRepoPath,
  revalidateResolvedRepoPath,
  type ResolvedRepoPath,
} from "../../agent/tools/repoPathSafety.js";
import { carriedFailureCategory, safeErrorMessage, withFailureCategory } from "../../agent/captureGuards.js";
import { grantSecrets, redactSecrets } from "../../redact.js";
import { buildScrubbedEnv } from "./agentSdkRunner.js";
import { scrubSecrets } from "./envScrub.js";
import { composeRunnerCapabilities } from "./runnerCapabilities.js";
import { createStandardServicesFacade } from "./standardServicesFacade.js";
import type {
  StandardServiceEnvironment,
  StandardServiceIdentity,
  StandardServiceProvider,
} from "./standardServices.js";

// ---------------------------------------------------------------- contract //

export interface RepoEditInput {
  /** The operator's instruction, verbatim (e.g. "change the ui to light mode"). */
  instruction: string;
  /** Absolute path of the consumer's mushy repo checkout. */
  repoDir: string;
  /** The Agent SDK credential (injected; never ambient). */
  grant: AgentGrant;
  /** Optional extra context the shell wants the editor to know (routes, conventions). */
  context?: string;
  /** Cooperative cancellation supplied by a request scheduler or client. */
  signal?: AbortSignal;
  /**
   * Optional live progress sink: the model's text as it streams
   * (`text_delta`), every repo tool invocation as it happens (`tool`), and
   * turn boundaries (`turn`). Lets a shell show the session actually working
   * instead of an opaque spinner. Events are advisory; throwing from the
   * callback is swallowed.
   */
  onEvent?: (event: RepoEditEvent) => void;
}

export type RepoEditEvent =
  | { type: "text_delta"; text: string }
  | { type: "tool"; tool: "list" | "read" | "grep" | "write" | "edit" | "delete" | "history"; path: string }
  | { type: "turn"; count: number };

/**
 * Apply ONE exact-match surgical replacement to a repo file (the repo_edit
 * tool's engine, exported for hermetic tests). Mirrors the Edit-tool contract:
 * old_string must occur EXACTLY ONCE unless replaceAll; a zero or ambiguous
 * match is an error, never a guess. Timing rationale: the write-only toolset
 * forced whole-file regeneration — a measured 130s button edit spent ~60s
 * re-emitting 17KB of unchanged bytes (and 20s composing a surgical edit the
 * permission gate then denied). This tool IS that surgical path, contained.
 */
export function applyRepoEdit(
  repoDir: string,
  relPath: string,
  oldString: string,
  newString: string,
  replaceAll = false,
): { occurrences: number; changed: boolean } {
  const target = operationPath(repoDir, relPath);
  const fd = openSync(target.path, "r+");
  try {
    assertOpenedFileIdentity(target, fstatSync(fd));
    const before = readFileSync(fd, "utf8");
    if (!oldString) throw new Error("old_string must be non-empty");
    const occurrences = before.split(oldString).length - 1;
    if (occurrences === 0) throw new Error("old_string not found (must match the file exactly)");
    if (occurrences > 1 && !replaceAll) {
      throw new Error(`old_string matches ${occurrences} times; make it unique or set replace_all`);
    }
    const changed = oldString !== newString;
    if (changed) {
      const bytes = Buffer.from(before.split(oldString).join(newString), "utf8");
      ftruncateSync(fd, 0);
      writeSync(fd, bytes, 0, bytes.length, 0);
    }
    return { occurrences, changed };
  } finally {
    closeSync(fd);
  }
}

/** Refuse a tool-loop exhaustion that never produced the SDK's final result. */
export function assertRepoEditTerminalResult(sawResult: boolean, changedFiles: string[]): void {
  if (sawResult) return;
  const touched = changedFiles.length
    ? ` after touching ${changedFiles.join(", ")}`
    : "";
  throw new Error(
    `in-app edit session ended without a terminal SDK result${touched}; changes were not committed`,
  );
}

export interface RepoEditResult {
  /** The editor's final summary of what it did. */
  summary: string;
  /** Repo-relative paths written or deleted this session, sorted. */
  changedFiles: string[];
  /** True when the model invoked a mutating tool, even if it was a no-op. */
  attemptedWrites?: boolean;
  /** Bounded inspection usage for operator diagnostics. */
  inspection?: {
    bytesRead: number;
    matches: number;
    truncated: boolean;
  };
  telemetry?: AuthorTelemetry;
}

export interface RepoEditor {
  edit(input: RepoEditInput): Promise<RepoEditResult>;
}

// ------------------------------------------------------------- containment //

/**
 * Resolve a repo-relative reference or return null. Plain relative paths
 * only; never `.git` (repo integrity is the shell's, not the model's).
 * Exported for the shell's own file endpoints and for hermetic tests.
 */
export function safeRepoRelative(repoDir: string, rel: unknown): string | null {
  return resolveContainedRepoPath(repoDir, rel)?.path ?? null;
}

function operationPath(repoDir: string, relPath: string): ResolvedRepoPath {
  const target = resolveContainedRepoPath(repoDir, relPath);
  if (!target) throw new Error(`path escapes the repo: ${relPath}`);
  return revalidateResolvedRepoPath(target);
}

/**
 * Atomically detach one validated file from its caller-visible name before
 * unlinking it. The optional seam exists only for a deterministic race test.
 */
export function quarantineDeleteRepoFile(
  repoDir: string,
  relPath: string,
  beforeRename?: () => void,
): void {
  let target = operationPath(repoDir, relPath);
  const final = target.components.at(-1);
  if (!final || final.kind !== "file") throw new Error("repo_delete path must be a regular file");
  const normalized = relPath.replaceAll("\\", "/");
  const parentRel = normalized.includes("/") ? normalized.slice(0, normalized.lastIndexOf("/")) : ".";
  let parent = operationPath(repoDir, parentRel || ".");
  const quarantine = mkdtempSync(join(parent.path, ".mushy-delete-"));
  const quarantineRel = parentRel === "."
    ? basename(quarantine)
    : `${parentRel}/${basename(quarantine)}`;
  let quarantineTarget = operationPath(repoDir, quarantineRel);
  const destination = join(quarantine, "entry");
  let moved = false;
  let preserveEvidence = false;
  let admittedFd = -1;
  try {
    parent = revalidateResolvedRepoPath(parent);
    target = revalidateResolvedRepoPath(target);
    quarantineTarget = revalidateResolvedRepoPath(quarantineTarget);
    // Keep the admitted inode alive across the path race. Without this handle,
    // Linux may reuse the inode immediately after unlink, making a replacement
    // look identical even though it is different content.
    admittedFd = openSync(target.path, "r");
    assertOpenedFileIdentity(target, fstatSync(admittedFd));
    if (lstatSync(destination, { throwIfNoEntry: false })) {
      preserveEvidence = true;
      throw new Error(`repo_delete quarantine destination is not exclusive; evidence preserved at ${quarantineRel}/entry`);
    }
    beforeRename?.();
    renameSync(target.path, destination);
    moved = true;
    try {
      assertOpenedFileIdentity(target, lstatSync(destination));
    } catch {
      preserveEvidence = true;
      throw new Error(`repo_delete identity changed; unproven entry preserved at ${quarantineRel}/entry`);
    }
    rmSync(destination);
    moved = false;
  } finally {
    if (admittedFd >= 0) closeSync(admittedFd);
    // Never unlink an entry whose identity did not match the admitted target.
    // A mismatch remains in the contained quarantine for operator recovery.
    if (!moved && !preserveEvidence) rmdirSync(quarantine);
  }
}

export interface RepoReadRange {
  path: string;
  startLine: number;
  endLine: number;
  totalLines: number;
  text: string;
  bytesReturned: number;
  truncated: boolean;
}

/** Hard input ceiling for one ranged read. Output has its own smaller cap. */
export const REPO_READ_MAX_INPUT_BYTES = 8_388_608;

function readRepoFileWithinInputCap(target: ResolvedRepoPath): string {
  const fd = openSync(target.path, "r");
  try {
    const stat = fstatSync(fd);
    assertOpenedFileIdentity(target, stat);
    if (stat.size > REPO_READ_MAX_INPUT_BYTES) {
      throw new Error(
        `repo_read input exceeds ${REPO_READ_MAX_INPUT_BYTES}-byte cap; narrow the source file before reading it`,
      );
    }

    const bytes = Buffer.allocUnsafe(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const count = readSync(fd, bytes, offset, bytes.length - offset, offset);
      if (count === 0) break;
      offset += count;
    }

    // Fail closed if a concurrent writer grew the file after fstatSync. The
    // extra one-byte probe keeps the allocation and decoded input bounded.
    const probe = Buffer.allocUnsafe(1);
    if (readSync(fd, probe, 0, 1, offset) > 0) {
      throw new Error(
        `repo_read input changed during inspection or exceeds ${REPO_READ_MAX_INPUT_BYTES}-byte cap; retry the read`,
      );
    }
    return bytes.subarray(0, offset).toString("utf8");
  } finally {
    closeSync(fd);
  }
}

/** Read a bounded, one-based line range with explicit truncation metadata. */
export function readRepoFileRange(
  repoDir: string,
  relPath: string,
  options: { startLine?: number; endLine?: number; maxBytes?: number } = {},
): RepoReadRange {
  const target = operationPath(repoDir, relPath);
  const source = readRepoFileWithinInputCap(target);
  const lines = source.split(/\r?\n/);
  const totalLines = lines.length;
  const startLine = Math.max(1, Math.trunc(options.startLine ?? 1));
  const requestedEnd = Math.max(startLine, Math.trunc(options.endLine ?? (startLine + 199)));
  const endLimit = Math.min(totalLines, requestedEnd);
  const maxBytes = Math.max(1, Math.min(262_144, Math.trunc(options.maxBytes ?? 65_536)));
  const chunks: string[] = [];
  let bytesReturned = 0;
  let lastLine = startLine - 1;
  let byteTruncated = false;
  for (let lineNo = startLine; lineNo <= endLimit; lineNo += 1) {
    const value = lines[lineNo - 1] ?? "";
    const prefix = chunks.length ? "\n" : "";
    const encoded = Buffer.from(prefix + value, "utf8");
    if (bytesReturned + encoded.length > maxBytes) {
      const remaining = maxBytes - bytesReturned;
      if (remaining > 0) chunks.push(encoded.subarray(0, remaining).toString("utf8"));
      bytesReturned = maxBytes;
      lastLine = lineNo;
      byteTruncated = true;
      break;
    }
    chunks.push(prefix + value);
    bytesReturned += encoded.length;
    lastLine = lineNo;
  }
  return {
    path: relPath.replaceAll("\\", "/"),
    startLine,
    endLine: Math.max(startLine - 1, lastLine),
    totalLines,
    text: chunks.join(""),
    bytesReturned,
    truncated: byteTruncated || lastLine < totalLines || requestedEnd < totalLines,
  };
}

export interface RepoGrepResult {
  query: string;
  matches: Array<{ path: string; line: number; text: string; contextBefore: string[]; contextAfter: string[] }>;
  filesScanned: number;
  bytesScanned: number;
  truncated: boolean;
}

/** Literal, recursive, bounded repository search with small context windows. */
export function grepRepoFiles(
  repoDir: string,
  options: {
    query: string;
    path?: string;
    maxMatches?: number;
    contextLines?: number;
    maxBytes?: number;
  },
): RepoGrepResult {
  if (!options.query) throw new Error("query must be non-empty");
  const rootRel = options.path?.trim() || ".";
  const root = operationPath(repoDir, rootRel);
  const maxMatches = Math.max(1, Math.min(500, Math.trunc(options.maxMatches ?? 100)));
  const contextLines = Math.max(0, Math.min(5, Math.trunc(options.contextLines ?? 2)));
  const maxBytes = Math.max(1, Math.min(8_388_608, Math.trunc(options.maxBytes ?? 2_097_152)));
  const matches: RepoGrepResult["matches"] = [];
  let filesScanned = 0;
  let bytesScanned = 0;
  let truncated = false;
  const queue: Array<{ target: ResolvedRepoPath; rel: string }> = [{
    target: root,
    rel: rootRel === "." ? "" : rootRel.replaceAll("\\", "/"),
  }];
  while (queue.length && !truncated) {
    const current = queue.shift()!;
    const target = revalidateResolvedRepoPath(current.target);
    const stat = lstatSync(target.path);
    if (stat.isDirectory()) {
      for (const name of readdirSync(target.path).sort()) {
        if (name === ".git" || name === "__pycache__" || name === "node_modules") continue;
        const rel = current.rel ? `${current.rel}/${name}` : name;
        const child = resolveContainedRepoPath(repoDir, rel);
        if (child) queue.push({ target: child, rel });
      }
      continue;
    }
    if (!stat.isFile()) continue;
    if (bytesScanned + stat.size > maxBytes) {
      truncated = true;
      break;
    }
    const source = readRepoFileWithinInputCap(target);
    bytesScanned += Buffer.byteLength(source, "utf8");
    filesScanned += 1;
    const lines = source.split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      if (!lines[i]!.includes(options.query)) continue;
      matches.push({
        path: current.rel,
        line: i + 1,
        text: lines[i]!,
        contextBefore: lines.slice(Math.max(0, i - contextLines), i),
        contextAfter: lines.slice(i + 1, i + 1 + contextLines),
      });
      if (matches.length >= maxMatches) {
        truncated = true;
        break;
      }
    }
  }
  return { query: options.query, matches, filesScanned, bytesScanned, truncated };
}

const EDITOR_SYSTEM_PROMPT = `You are the in-app editor of a MUSHY application.
The entire soft substance of the app lives in this git repository:
- ui/            the app's frontend, served LIVE from the repo (edit ui/index.html and a reload shows it)
- tools/<name>/  deterministic capability artifacts: tool.py exposing run(intake, params) -> (result, overlay)
- registry.json  {"tools":[{name, description, engine_op, params, returns, capabilities, kind:"script", version, provenance, entry:"tools/<name>/tool.py"}]} — the catalog, folded at call time
Apply the operator's instruction by editing files with the repo tools you are given.
Your toolset is CLOSED and exact: repo_list, repo_read, repo_grep, repo_edit, repo_write,
repo_delete, repo_history. No other tool exists in this session — never search
for more, and never attempt native file/shell tools (they are denied).
repo_read is a bounded ranged reader. Use start_line/end_line to inspect large
files in pieces. Use repo_grep for exact text or symbol discovery. Both return
explicit truncation metadata; continue with another range when truncated.
PREFER repo_edit (exact-match surgical replacement) for changes to existing
files; repo_write is for NEW files or full rewrites the instruction demands.
Keep edits minimal and complete; do not invent files outside the instruction's scope.
For questions about the app's own history (when something landed, what changed,
how long an edit took), use repo_history: every commit is one authored change,
a commit IS its deployment, and chat-edit commits carry Edit-Wall-Ms /
Edit-Cost-USD trailers. Answering a question with no edits is a fine outcome.
The shell commits your changes after you finish — do not attempt git operations.
End with a short plain summary of what you changed (or found).`;

/**
 * Read-only commit history for provenance questions — exported so shells and
 * tests use the exact reader the edit session gets. Fixed argv, scrubbed env,
 * bounded count; body included so provenance trailers are visible.
 */
export function readRepoHistory(repoDir: string, limit = 30): Array<{
  sha: string; author: string; when: string; subject: string; body: string;
}> {
  const n = Math.max(1, Math.min(200, Math.trunc(limit)));
  const raw = execFileSync("git",
    ["-C", repoDir, "log", `-n${n}`, "--format=%H%x1f%an <%ae>%x1f%aI%x1f%s%x1f%b%x1e"],
    { encoding: "utf8", env: scrubSecrets(process.env) });
  return raw.split("\x1e").map((r) => r.trim()).filter(Boolean).map((rec) => {
    const [sha, author, when, subject, body] = rec.split("\x1f");
    return { sha, author, when, subject, body: (body ?? "").trim() };
  });
}

// ------------------------------------------------------------ SDK plumbing //

interface CallToolResult {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
}

interface SdkModule {
  query(args: { prompt: string; options: Record<string, unknown> }): AsyncIterable<Record<string, unknown>>;
  tool(name: string, description: string, schema: unknown, handler: (args: Record<string, unknown>) => Promise<CallToolResult>): unknown;
  createSdkMcpServer(cfg: { name: string; version: string; tools: unknown[] }): unknown;
}

function dynImport(parts: string[]): Promise<unknown> {
  return import(parts.join("/"));
}

export interface SdkRepoEditorOptions {
  model?: string;
  maxTurns?: number;
  maxWallTimeMs?: number;
  standardServices?: {
    provider: StandardServiceProvider;
    identity: StandardServiceIdentity | ((input: RepoEditInput) => StandardServiceIdentity);
    environment: StandardServiceEnvironment;
  };
  /** Test seams. Production uses the installed Agent SDK and zod. */
  sdkImport?: () => Promise<unknown>;
  zodImport?: () => Promise<unknown>;
}

export class SdkRepoEditor implements RepoEditor {
  constructor(private readonly opts: SdkRepoEditorOptions = {}) {}

  /**
   * Scrub the grant out of anything that escapes.
   *
   * This runner injects the tenant's grant into the SDK's child env, and the
   * SDK's own faults quote the offending value: Node/undici header validation
   * renders "Invalid header value: <the credential>". The shell that catches
   * whatever escapes writes it to the DURABLE conversation log
   * (examples/webapp/server.mjs `remember`), serves it from /api/chat/log, and
   * feeds it back to later models through `conversationContext()` — so an
   * unredacted fault would persist AND republish the caller's token.
   *
   * Scrub this run's ACTUAL grant value, not just token-shaped text: a
   * credential can clear the app's 24-char floor while still being too short
   * for TOKENISH, so the pattern pass alone would leak it. Same shape as
   * ConverseSdkRunner.run (sol-critic PR #117 round 2, blocker 1); this is the
   * repo-edit lane's copy of it (sol-critic PR #4, finding 1).
   *
   * It carries the failure brand across the rewrap for the same reason its twin
   * does -- see `AgentSdkRunner.run` for the full account. `editInner` brands
   * nothing TODAY, so on this lane the carry is currently a no-op. It is written
   * anyway because the twin's bug was created by exactly this asymmetry: a
   * wrapper added for redaction, a brand added later in the inner function, and
   * no textual conflict between them to make the loss visible. A `withFailureCategory`
   * added below this line will now survive rather than be silently dropped.
  */
  async edit(input: RepoEditInput): Promise<RepoEditResult> {
    try {
      return await this.editInner(input);
    } catch (e) {
      const scrubbed = redactSecrets(
        safeErrorMessage(e),
        grantSecrets(input.grant),
      );
      const wrapped = new Error(scrubbed);
      // Drop the original stack: it can quote the offending value too.
      wrapped.stack = `${wrapped.name}: ${scrubbed}`;
      const carried = carriedFailureCategory(e);
      throw carried ? withFailureCategory(wrapped, carried) : wrapped;
    }
  }

  private async editInner(input: RepoEditInput): Promise<RepoEditResult> {
    const sdk = (await (this.opts.sdkImport?.() ?? dynImport(["@anthropic-ai", "claude-agent-sdk"]))) as SdkModule;
    const { z } = (await (this.opts.zodImport?.() ?? dynImport(["zod"]))) as { z: Record<string, (...a: never[]) => unknown> & {
      enum(v: readonly string[]): unknown;
      string(): unknown;
      number(): unknown;
      boolean(): unknown;
      record(inner: unknown): unknown;
      unknown(): unknown;
    } };
    type IntegerSchema = {
      int(): IntegerSchema;
      min(value: number): IntegerSchema;
      max(value: number): IntegerSchema;
      default(value: number): unknown;
    };
    const boundedInteger = (minimum: number, maximum: number, fallback: number): unknown =>
      (z.number() as IntegerSchema).int().min(minimum).max(maximum).default(fallback);
    const childEnv = buildScrubbedEnv(input.grant, process.env);
    const changed = new Set<string>();
    let attemptedWrites = false;
    let inspectionBytes = 0;
    let inspectionMatches = 0;
    let inspectionTruncated = false;
    const ok = (text: string): CallToolResult => ({ content: [{ type: "text", text }] });
    const bad = (text: string): CallToolResult => ({ content: [{ type: "text", text }], isError: true });
    const emit = (event: RepoEditEvent): void => {
      try { input.onEvent?.(event); } catch { /* progress is advisory */ }
    };

    const tools = [
      sdk.tool("repo_list", "List a repo directory (repo-relative path; '' = root). Returns name+kind entries.",
        { path: z.string() },
        async (a) => {
          emit({ type: "tool", tool: "list", path: String(a.path ?? "") || "." });
          try {
            const target = operationPath(input.repoDir, String(a.path ?? "") || ".");
            return ok(JSON.stringify(readdirSync(target.path)
              .filter((n) => !isWindowsUnsafeRepoComponent(n) && n !== "__pycache__")
              .map((n) => ({ name: n, kind: lstatSync(join(target.path, n)).isDirectory() ? "dir" : "file" }))));
          } catch (e) { return bad(`list error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_read", "Read a bounded line range from a repo file. Returns text plus total lines, returned byte count, and explicit truncation metadata.",
        { path: z.string(),
          start_line: boundedInteger(1, 10_000_000, 1),
          end_line: boundedInteger(1, 10_000_000, 200),
          max_bytes: boundedInteger(1, 262_144, 65_536) },
        async (a) => {
          emit({ type: "tool", tool: "read", path: String(a.path) });
          try {
            const result = readRepoFileRange(input.repoDir, String(a.path), {
              startLine: a.start_line as number,
              endLine: a.end_line as number,
              maxBytes: a.max_bytes as number,
            });
            inspectionBytes += result.bytesReturned;
            inspectionTruncated ||= result.truncated;
            return ok(JSON.stringify(result));
          }
          catch (e) { return bad(`read error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_grep", "Search for exact text or a symbol across bounded repo files. Returns line numbers, small context windows, scan counts, and explicit truncation metadata.",
        { query: z.string(),
          path: (z.string() as { optional(): unknown }).optional(),
          max_matches: boundedInteger(1, 500, 100),
          context_lines: boundedInteger(0, 5, 2),
          max_bytes: boundedInteger(1, 8_388_608, 2_097_152) },
        async (a) => {
          emit({ type: "tool", tool: "grep", path: String(a.path ?? ".") });
          try {
            const result = grepRepoFiles(input.repoDir, {
              query: String(a.query ?? ""),
              path: String(a.path ?? "."),
              maxMatches: a.max_matches as number,
              contextLines: a.context_lines as number,
              maxBytes: a.max_bytes as number,
            });
            inspectionBytes += result.bytesScanned;
            inspectionMatches += result.matches.length;
            inspectionTruncated ||= result.truncated;
            return ok(JSON.stringify(result));
          } catch (e) { return bad(`grep error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_write", "Create or overwrite ONE repo file with exact full content (repo-relative path; parent dirs are created).",
        { path: z.string(), content: z.string() },
        async (a) => {
          attemptedWrites = true;
          emit({ type: "tool", tool: "write", path: String(a.path) });
          try {
            let target = operationPath(input.repoDir, String(a.path));
            mkdirSync(dirname(target.path), { recursive: true });
            target = operationPath(input.repoDir, String(a.path));
            const final = target.components.at(-1);
            const flags = final?.path === target.path ? "r+" : "wx";
            const fd = openSync(target.path, flags);
            try {
              const stat = fstatSync(fd);
              if (flags === "r+") assertOpenedFileIdentity(target, stat);
              else if (!stat.isFile() || stat.nlink !== 1) {
                throw new Error("repository file identity changed before operation");
              }
              const bytes = Buffer.from(String(a.content ?? ""), "utf8");
              if (flags === "r+") ftruncateSync(fd, 0);
              writeSync(fd, bytes, 0, bytes.length, 0);
            } finally {
              closeSync(fd);
            }
            changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(`wrote ${a.path}`);
          } catch (e) { return bad(`write error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_edit", "Surgically replace text in ONE repo file: old_string must match the file content EXACTLY ONCE (or set replace_all to 'true'). STRONGLY PREFER this over repo_write for changes to an existing file — never regenerate a whole file to change part of it.",
        { path: z.string(), old_string: z.string(), new_string: z.string(),
          replace_all: (z.boolean() as { default(value: boolean): unknown }).default(false) },
        async (a) => {
          attemptedWrites = true;
          emit({ type: "tool", tool: "edit", path: String(a.path) });
          try {
            const { occurrences, changed: didChange } = applyRepoEdit(
              input.repoDir, String(a.path), String(a.old_string ?? ""),
              String(a.new_string ?? ""), a.replace_all === true);
            if (didChange) changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(didChange
              ? `edited ${a.path} (${occurrences} replacement${occurrences === 1 ? "" : "s"})`
              : `no change to ${a.path} (old_string and new_string are identical)`);
          } catch (e) { return bad(`edit error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_delete", "Delete ONE repo file (repo-relative path).",
        { path: z.string() },
        async (a) => {
          attemptedWrites = true;
          emit({ type: "tool", tool: "delete", path: String(a.path) });
          try {
            quarantineDeleteRepoFile(input.repoDir, String(a.path));
            changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(`deleted ${a.path}`);
          } catch (e) { return bad(`delete error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_history", "READ-ONLY commit history of this repo (newest first): sha, author, ISO date, subject, body (chat-edit commits carry Edit-Wall-Ms / Edit-Cost-USD trailers). Use for provenance questions: when something landed, what an edit was, how long it took.",
        { limit: boundedInteger(1, 200, 30) },
        async (a) => {
          emit({ type: "tool", tool: "history", path: "git log" });
          try {
            return ok(JSON.stringify(readRepoHistory(input.repoDir, a.limit as number)));
          } catch (e) { return bad(`history error: ${(e as Error).message}`); }
        }),
    ];

    const server = sdk.createSdkMcpServer({ name: "repo", version: "1.0.0", tools });
    const allowedNames = ["mcp__repo__repo_list", "mcp__repo__repo_read", "mcp__repo__repo_grep", "mcp__repo__repo_edit", "mcp__repo__repo_write", "mcp__repo__repo_delete", "mcp__repo__repo_history"];
    const services = this.opts.standardServices
      ? createStandardServicesFacade({
          sdk,
          z,
          provider: this.opts.standardServices.provider,
          identity: typeof this.opts.standardServices.identity === "function"
            ? this.opts.standardServices.identity(input)
            : this.opts.standardServices.identity,
          environment: this.opts.standardServices.environment,
          profile: "shell-editor",
        })
      : undefined;
    const composition = composeRunnerCapabilities({
      profile: "shell-editor",
      private_mcp_servers: { repo: server },
      private_allowed_tools: allowedNames,
      private_disallowed_tools: [
        "Bash", "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit",
        "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite",
        "ToolSearch", "KillShell", "TaskOutput",
      ],
      ...(services ? { services } : {}),
    });
    const allowed = new Set(composition.allowedTools ?? []);
    const abort = new AbortController();
    const abortFromCaller = () => abort.abort();
    if (input.signal?.aborted) abort.abort();
    else input.signal?.addEventListener("abort", abortFromCaller, { once: true });
    const maxWall = this.opts.maxWallTimeMs ?? 480_000;
    let wallCapHit = false;
    const wallTimer = setTimeout(() => { wallCapHit = true; abort.abort(); }, maxWall);

    let summary = "";
    let sawResult = false;
    let telemetry: AuthorTelemetry | undefined;
    try {
      const q = sdk.query({
        prompt: `${EDITOR_SYSTEM_PROMPT}${input.context ? `\n\nShell context:\n${input.context}` : ""}\n\nOperator instruction:\n${input.instruction}`,
        options: {
          env: childEnv,
          ...(this.opts.model ? { model: this.opts.model } : {}),
          maxTurns: this.opts.maxTurns ?? 24,
          settingSources: [],
          permissionMode: "default",
          includePartialMessages: true,
          cwd: input.repoDir,
          abortController: abort,
          mcpServers: composition.mcpServers,
          allowedTools: composition.allowedTools,
          // Observed 2026-08-06: native READ-ONLY tools and Bash executed
          // without consulting canUseTool (auto-allowed below the permission
          // layer), so the repo-scope containment claim held only for writes.
          // Deny the native surface outright — the closed repo_* set is the
          // whole contract, and repo_edit removes the speed reason to want
          // native Edit.
          disallowedTools: composition.disallowedTools,
          canUseTool: async (toolName: string, inp: Record<string, unknown>) =>
            allowed.has(toolName)
              ? { behavior: "allow" as const, updatedInput: inp }
              : { behavior: "deny" as const, message: `only the repo_* and mounted services tools are permitted in the in-app edit session (got ${toolName})` },
        },
      });
      let turns = 0;
      for await (const msg of q) {
        const type = String((msg as { type?: unknown }).type ?? "");
        if (type === "stream_event") {
          // TRUE streaming (same shape converseSdkRunner consumes): only
          // content_block_delta text_delta becomes progress text.
          const ev = ((msg as { event?: unknown }).event ?? {}) as Record<string, unknown>;
          if (ev.type === "content_block_delta") {
            const delta = (ev.delta ?? {}) as Record<string, unknown>;
            if (delta.type === "text_delta" && typeof delta.text === "string" && delta.text) {
              emit({ type: "text_delta", text: delta.text });
            }
          }
        } else if (type === "assistant") {
          turns += 1;
          emit({ type: "turn", count: turns });
        } else if (type === "result") {
          sawResult = true;
          const m = msg as { result?: unknown; usage?: Record<string, unknown>; total_cost_usd?: unknown; num_turns?: unknown; modelUsage?: Record<string, unknown> };
          summary = typeof m.result === "string" ? m.result : summary;
          const u = m.usage ?? {};
          telemetry = {
            ...(typeof m.num_turns === "number" ? { turns: m.num_turns } : {}),
            ...(typeof u.input_tokens === "number" ? { input_tokens: u.input_tokens } : {}),
            ...(typeof u.output_tokens === "number" ? { output_tokens: u.output_tokens } : {}),
            ...(typeof m.total_cost_usd === "number" ? { total_cost_usd: m.total_cost_usd } : {}),
            ...(m.modelUsage ? { models: Object.keys(m.modelUsage) } : {}),
          };
        }
      }
    } finally {
      clearTimeout(wallTimer);
      input.signal?.removeEventListener("abort", abortFromCaller);
    }
    if (input.signal?.aborted) {
      throw new Error("in-app edit session was cancelled");
    }
    if (wallCapHit) {
      throw new Error(
        `in-app edit session hit the ${Math.round(maxWall / 1000)}s wall-time cap` +
        (changed.size ? ` after touching ${[...changed].sort().join(", ")} (reset to last commit)` : "") +
        " — retry with a narrower instruction or raise maxWallTimeMs");
    }
    assertRepoEditTerminalResult(sawResult, [...changed].sort());
    if (!summary && changed.size === 0) {
      throw new Error("in-app edit session ended with no summary and no changes");
    }
    return {
      summary: summary || `edited: ${[...changed].sort().join(", ")}`,
      changedFiles: [...changed].sort(),
      attemptedWrites,
      inspection: {
        bytesRead: inspectionBytes,
        matches: inspectionMatches,
        truncated: inspectionTruncated,
      },
      ...(telemetry ? { telemetry } : {}),
    };
  }
}
