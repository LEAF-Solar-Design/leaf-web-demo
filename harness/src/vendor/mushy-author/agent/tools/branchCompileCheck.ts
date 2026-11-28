import { spawn } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { crc32 } from "node:zlib";
import { z } from "zod";

export interface BranchCompileDiagnostic {
  file: string;
  line: number;
  column: number;
  code: string;
  message: string;
}
export interface BranchCompileSummary {
  pass: boolean;
  decision: "accept" | "malformed" | "compile_failed" | "unavailable";
  errors: number;
}
type UnavailableCode = "not_configured" | "timeout" | "spawn_failed" | "bad_output" | "busy";
export type BranchCompileOutcome = (
  | { status: "accept"; allowlistSha256: string }
  | { status: "refused"; decision: "malformed" | "compile_failed"; diagnostics: string[]; structured: BranchCompileDiagnostic[] }
  | { status: "unavailable"; code: UnavailableCode }
) & { ms: number; summary: BranchCompileSummary };

export interface BranchCompileOptions {
  target?: "net8" | "net48";
  bin?: string;
  refs?: string;
  timeoutMs?: number;
  command?: string[];
}

export function branchCompileDisabled(env: NodeJS.ProcessEnv = process.env): boolean {
  return env.LEAF_BRANCH_COMPILE_CHECK === "off";
}

let testCheck: typeof branchCompileCheck | null = null;
let testNet48Check: typeof branchCompileCheck | null = null;
export function setBranchCompileCheckForTests(fn: typeof branchCompileCheck | null): void {
  testCheck = fn;
}

export function setBranchCompileNet48CheckForTests(fn: typeof branchCompileCheck | null): void {
  testNet48Check = fn;
}

export function branchHostFloor(outcome: BranchCompileOutcome): 2023 | 2025 | null {
  if (outcome.status === "accept") return 2023;
  if (outcome.status === "unavailable" && outcome.code === "not_configured") return null;
  return 2025;
}

export function branchCompileDecision(outcome: BranchCompileOutcome): string {
  return outcome.status === "unavailable" ? `unavailable:${outcome.code}` : outcome.summary.decision;
}

// ZIP32 with stored UTF-8 entries. The package validator bounds sizes and count.
function storedZip(packageName: string, files: Record<string, string>): Buffer {
  const records: Buffer[] = [];
  const directory: Buffer[] = [];
  let offset = 0;
  const paths = Object.keys(files).sort();
  for (const path of paths) {
    const name = Buffer.from(`${packageName}/${path}`);
    const content = Buffer.from(files[path]);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x800, 6);
    local.writeUInt16LE(0x21, 12); // 1980-01-01, earliest DOS date
    local.writeUInt32LE(crc32(content), 14);
    local.writeUInt32LE(content.length, 18);
    local.writeUInt32LE(content.length, 22);
    local.writeUInt16LE(name.length, 26);
    records.push(local, name, content);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    local.copy(central, 6, 4, 30);
    central.writeUInt32LE(offset, 42);
    directory.push(central, name);
    offset += local.length + name.length + content.length;
  }
  const index = Buffer.concat(directory);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(paths.length, 8);
  end.writeUInt16LE(paths.length, 10);
  end.writeUInt32LE(index.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...records, index, end]);
}

let running = 0;
const waiters = new Set<() => boolean>();
function acquire(deadline: number): Promise<boolean> {
  if (running < 2) {
    running++;
    return Promise.resolve(true);
  }
  return new Promise(resolve => {
    const take = () => {
      clearTimeout(timer);
      waiters.delete(take);
      const ready = performance.now() < deadline;
      resolve(ready);
      return ready;
    };
    const timer = setTimeout(() => {
      waiters.delete(take);
      resolve(false);
    }, Math.max(0, deadline - performance.now()));
    waiters.add(take);
  });
}
function release(): void {
  for (const take of waiters) if (take()) return;
  running--;
}

const hash = z.string().regex(/^[0-9a-f]{64}(?![\s\S])/i).nullable();
const report = z.strictObject({
  schema: z.literal("leaf.tool-check.v1"),
  ok: z.boolean(),
  decision: z.enum(["accept", "malformed", "compile_failed"]),
  diagnostics: z.array(z.string()).max(50),
  package_digest: hash,
  allowlist_sha256: hash,
  framework: z.string(),
});

function diagnostic(text: string): BranchCompileDiagnostic {
  const match = /^([A-Za-z0-9_./-]{1,200})\((\d{1,6}),(\d{1,6})\): (CS\d{4}|scan): (.{1,500})$/.exec(text);
  if (!match) return { file: "", line: 0, column: 0, code: "unparsed", message: text.slice(0, 500) };
  return { file: match[1], line: Number(match[2]), column: Number(match[3]), code: match[4], message: match[5] };
}
function unavailable(code: UnavailableCode, ms: number): BranchCompileOutcome {
  return { status: "unavailable", code, ms, summary: { pass: false, decision: "unavailable", errors: 0 } };
}

function parseOutput(stdout: Buffer, exit: number | null, ms: number): BranchCompileOutcome {
  try {
    const line = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(stdout).replace(/\r?\n$/, "");
    if (/[\r\n]/.test(line)) return unavailable("bad_output", ms);
    const doc = report.parse(JSON.parse(line));
    const { decision, diagnostics } = doc;
    if (exit === 0 && doc.ok && decision === "accept" && doc.allowlist_sha256) {
      return { status: "accept", allowlistSha256: doc.allowlist_sha256, ms, summary: { pass: true, decision, errors: 0 } };
    }
    if (exit === 1 && !doc.ok && decision !== "accept") {
      return { status: "refused", decision, diagnostics, structured: diagnostics.map(diagnostic), ms,
        summary: { pass: false, decision, errors: diagnostics.length } };
    }
  } catch { /* Malformed UTF-8, JSON and schema violations all fail closed. */ }
  return unavailable("bad_output", ms);
}

async function runChecker(command: string[], dir: string, refs: string, start: number, deadline: number): Promise<BranchCompileOutcome> {
  const elapsed = () => Math.round(performance.now() - start);
  const remaining = deadline - performance.now();
  if (remaining <= 0) return unavailable("timeout", elapsed());
  const env: NodeJS.ProcessEnv = {
    PATH: process.env.PATH ?? "", HOME: dir, NUGET_PACKAGES: refs,
    DOTNET_CLI_TELEMETRY_OPTOUT: "1", DOTNET_NOLOGO: "1", DOTNET_gcServer: "0",
  };
  // Node otherwise copies its coverage destination from the parent even with
  // an explicit env. An undefined own property suppresses that implicit copy.
  Object.defineProperty(env, "NODE_V8_COVERAGE", { value: undefined });
  const child = spawn(command[0], [...command.slice(1), "--package", join(dir, "package.zip")], {
    cwd: dir, windowsHide: true, shell: false, stdio: ["ignore", "pipe", "ignore"],
    env,
  });
  return new Promise(resolve => {
    const chunks: Buffer[] = [];
    let size = 0;
    let failure: UnavailableCode | undefined;
    const stop = (code: UnavailableCode) => {
      if (failure) return;
      failure = code;
      child.kill("SIGKILL");
    };
    const timer = setTimeout(() => stop("timeout"), remaining);
    child.stdout.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > 1024 * 1024) stop("bad_output");
      else chunks.push(chunk);
    });
    child.stdout.on("error", () => stop("bad_output"));
    child.on("error", () => { failure ??= "spawn_failed"; });
    child.once("close", (exit, signal) => {
      clearTimeout(timer);
      if (performance.now() >= deadline) failure ??= "timeout";
      resolve(failure || signal ? unavailable(failure ?? "bad_output", elapsed())
        : parseOutput(Buffer.concat(chunks), exit, elapsed()));
    });
  });
}

export async function branchCompileCheck(packageName: string, files: Record<string, string>, opts: BranchCompileOptions = {}): Promise<BranchCompileOutcome> {
  const check = opts.target === "net48" ? testNet48Check : testCheck;
  if (check) {
    try { return await check(packageName, files, opts); }
    catch (error) {
      if (opts.target !== "net48") throw error;
      return unavailable("spawn_failed", 0);
    }
  }
  const start = performance.now();
  const deadline = start + (opts.timeoutMs ?? 30_000);
  const bin = opts.bin ?? (opts.target === "net48" ? process.env.LEAF_BRANCH_COMPILE_NET48_BIN : process.env.LEAF_BRANCH_COMPILE_BIN);
  const refs = opts.refs ?? process.env.LEAF_BRANCH_COMPILE_REFS;
  if (!bin || !refs) return unavailable("not_configured", 0);
  if (!await acquire(deadline)) return unavailable("busy", Math.round(performance.now() - start));
  try {
    const dir = await mkdtemp(join(tmpdir(), "branch-compile-"));
    try {
      await writeFile(join(dir, "package.zip"), storedZip(packageName, files));
      return await runChecker(opts.command ?? [bin], dir, refs, start, deadline);
    } finally {
      await rm(dir, { recursive: true, force: true, maxRetries: 3 });
    }
  } catch {
    return unavailable("spawn_failed", Math.round(performance.now() - start));
  } finally {
    release();
  }
}
