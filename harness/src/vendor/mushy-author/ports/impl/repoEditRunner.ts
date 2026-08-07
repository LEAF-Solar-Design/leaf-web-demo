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
import { lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve, sep } from "node:path";
import type { AgentGrant, AuthorTelemetry } from "../index.js";
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
  | { type: "tool"; tool: "list" | "read" | "write" | "edit" | "delete" | "history"; path: string }
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
): { occurrences: number } {
  const abs = safeRepoRelative(repoDir, relPath);
  if (!abs) throw new Error(`path escapes the repo: ${relPath}`);
  const before = readFileSync(abs, "utf8");
  if (!oldString) throw new Error("old_string must be non-empty");
  const occurrences = before.split(oldString).length - 1;
  if (occurrences === 0) throw new Error("old_string not found (must match the file exactly)");
  if (occurrences > 1 && !replaceAll) {
    throw new Error(`old_string matches ${occurrences} times; make it unique or set replace_all`);
  }
  writeFileSync(abs, before.split(oldString).join(newString), "utf8");
  return { occurrences };
}

export interface RepoEditResult {
  /** The editor's final summary of what it did. */
  summary: string;
  /** Repo-relative paths written or deleted this session, sorted. */
  changedFiles: string[];
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
  if (typeof rel !== "string" || !rel.trim() || rel.includes("\0")) return null;
  const norm = rel.replaceAll("\\", "/").replace(/^\.\//, "");
  if (norm.startsWith("/") || /^[A-Za-z]:/.test(norm)) return null;
  const parts = norm.split("/");
  if (parts.includes("..") || parts[0] === ".git") return null;
  const root = realpathSync(repoDir);
  const abs = resolve(root, norm);
  if (abs !== root && !abs.startsWith(root + sep)) return null;
  return abs;
}

const EDITOR_SYSTEM_PROMPT = `You are the in-app editor of a MUSHY application.
The entire soft substance of the app lives in this git repository:
- ui/            the app's frontend, served LIVE from the repo (edit ui/index.html and a reload shows it)
- tools/<name>/  deterministic capability artifacts: tool.py exposing run(intake, params) -> (result, overlay)
- registry.json  {"tools":[{name, description, engine_op, params, returns, capabilities, kind:"script", version, provenance, entry:"tools/<name>/tool.py"}]} — the catalog, folded at call time
Apply the operator's instruction by editing files with the repo tools you are given.
Your toolset is CLOSED and exact: repo_list, repo_read, repo_edit, repo_write,
repo_delete, repo_history. No other tool exists in this session — never search
for more, and never attempt native file/shell tools (they are denied).
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
  */
  async edit(input: RepoEditInput): Promise<RepoEditResult> {
    try {
      return await this.editInner(input);
    } catch (e) {
      const scrubbed = redactSecrets(
        e instanceof Error ? e.message : String(e),
        grantSecrets(input.grant),
      );
      const wrapped = new Error(scrubbed);
      // Drop the original stack: it can quote the offending value too.
      wrapped.stack = `${wrapped.name}: ${scrubbed}`;
      throw wrapped;
    }
  }

  private async editInner(input: RepoEditInput): Promise<RepoEditResult> {
    const sdk = (await (this.opts.sdkImport?.() ?? dynImport(["@anthropic-ai", "claude-agent-sdk"]))) as SdkModule;
    const { z } = (await (this.opts.zodImport?.() ?? dynImport(["zod"]))) as { z: Record<string, (...a: never[]) => unknown> & {
      enum(v: readonly string[]): unknown;
      string(): unknown;
      record(inner: unknown): unknown;
      unknown(): unknown;
    } };
    const childEnv = buildScrubbedEnv(input.grant, process.env);
    const changed = new Set<string>();
    const ok = (text: string): CallToolResult => ({ content: [{ type: "text", text }] });
    const bad = (text: string): CallToolResult => ({ content: [{ type: "text", text }], isError: true });
    const contained = (rel: unknown): string | null => safeRepoRelative(input.repoDir, rel);
    const emit = (event: RepoEditEvent): void => {
      try { input.onEvent?.(event); } catch { /* progress is advisory */ }
    };

    const tools = [
      sdk.tool("repo_list", "List a repo directory (repo-relative path; '' = root). Returns name+kind entries.",
        { path: z.string() },
        async (a) => {
          const abs = contained(String(a.path ?? "") || ".");
          if (!abs) return bad("path escapes the repo");
          emit({ type: "tool", tool: "list", path: String(a.path ?? "") || "." });
          try {
            return ok(JSON.stringify(readdirSync(abs).filter((n) => n !== ".git" && n !== "__pycache__")
              .map((n) => ({ name: n, kind: lstatSync(join(abs, n)).isDirectory() ? "dir" : "file" }))));
          } catch (e) { return bad(`list error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_read", "Read a repo file (repo-relative path).",
        { path: z.string() },
        async (a) => {
          const abs = contained(a.path);
          if (!abs) return bad("path escapes the repo");
          emit({ type: "tool", tool: "read", path: String(a.path) });
          try { return ok(readFileSync(abs, "utf8")); }
          catch (e) { return bad(`read error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_write", "Create or overwrite ONE repo file with exact full content (repo-relative path; parent dirs are created).",
        { path: z.string(), content: z.string() },
        async (a) => {
          const abs = contained(a.path);
          if (!abs) return bad("path escapes the repo");
          emit({ type: "tool", tool: "write", path: String(a.path) });
          try {
            mkdirSync(dirname(abs), { recursive: true });
            writeFileSync(abs, String(a.content ?? ""), "utf8");
            changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(`wrote ${a.path}`);
          } catch (e) { return bad(`write error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_edit", "Surgically replace text in ONE repo file: old_string must match the file content EXACTLY ONCE (or set replace_all to 'true'). STRONGLY PREFER this over repo_write for changes to an existing file — never regenerate a whole file to change part of it.",
        { path: z.string(), old_string: z.string(), new_string: z.string(),
          replace_all: (z.string() as { optional(): unknown }).optional() },
        async (a) => {
          emit({ type: "tool", tool: "edit", path: String(a.path) });
          try {
            const { occurrences } = applyRepoEdit(
              input.repoDir, String(a.path), String(a.old_string ?? ""),
              String(a.new_string ?? ""), String(a.replace_all ?? "") === "true");
            changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(`edited ${a.path} (${occurrences} replacement${occurrences === 1 ? "" : "s"})`);
          } catch (e) { return bad(`edit error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_delete", "Delete ONE repo file (repo-relative path).",
        { path: z.string() },
        async (a) => {
          const abs = contained(a.path);
          if (!abs) return bad("path escapes the repo");
          emit({ type: "tool", tool: "delete", path: String(a.path) });
          try {
            rmSync(abs);
            changed.add(String(a.path).replaceAll("\\", "/"));
            return ok(`deleted ${a.path}`);
          } catch (e) { return bad(`delete error: ${(e as Error).message}`); }
        }),
      sdk.tool("repo_history", "READ-ONLY commit history of this repo (newest first): sha, author, ISO date, subject, body (chat-edit commits carry Edit-Wall-Ms / Edit-Cost-USD trailers). Use for provenance questions: when something landed, what an edit was, how long it took.",
        { limit: z.string() },
        async (a) => {
          emit({ type: "tool", tool: "history", path: "git log" });
          try {
            const n = Number.parseInt(String(a.limit ?? "30"), 10) || 30;
            return ok(JSON.stringify(readRepoHistory(input.repoDir, n)));
          } catch (e) { return bad(`history error: ${(e as Error).message}`); }
        }),
    ];

    const server = sdk.createSdkMcpServer({ name: "repo", version: "1.0.0", tools });
    const allowedNames = ["mcp__repo__repo_list", "mcp__repo__repo_read", "mcp__repo__repo_edit", "mcp__repo__repo_write", "mcp__repo__repo_delete", "mcp__repo__repo_history"];
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
    const maxWall = this.opts.maxWallTimeMs ?? 480_000;
    let wallCapHit = false;
    const wallTimer = setTimeout(() => { wallCapHit = true; abort.abort(); }, maxWall);

    let summary = "";
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
    }
    if (wallCapHit) {
      throw new Error(
        `in-app edit session hit the ${Math.round(maxWall / 1000)}s wall-time cap` +
        (changed.size ? ` after touching ${[...changed].sort().join(", ")} (reset to last commit)` : "") +
        " — retry with a narrower instruction or raise maxWallTimeMs");
    }
    if (!summary && changed.size === 0) {
      throw new Error("in-app edit session ended with no summary and no changes");
    }
    return {
      summary: summary || `edited: ${[...changed].sort().join(", ")}`,
      changedFiles: [...changed].sort(),
      ...(telemetry ? { telemetry } : {}),
    };
  }
}
