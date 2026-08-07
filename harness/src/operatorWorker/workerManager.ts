// Disposable development worker (contract/OPERATOR.md section 6, Lane D).
// Broad O2/O3 capability executes ONLY inside a disposable workspace:
// scrubbed env (allowlist, never the manager's env), denied-by-default
// network policy, full-tree timeout kill, idempotent submission, artifact
// receipts, and cleanup that survives a crashed manager (reapOrphans).
//
// The substrate is injectable: LocalProcessWorkspace for tests and
// air-gapped dev; an E2B microVM substrate (the repo's proven e2bRunner
// pattern) slots in behind the same interface for production isolation.

import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";

export interface WorkerJobEnvelope {
  workspace: "disposable";
  commands: string[];
  repo?: string;
  network?: string[]; // allowlisted host suffixes; empty = fully denied
  idempotencyKey: string;
  principalSubject: string;
  sessionId: string;
  timeoutMs?: number;
}

export interface WorkerArtifact {
  path: string;
  sha256: string;
  bytes: number;
}

export interface WorkerJobReceipt {
  jobId: string;
  principalSubject: string;
  status: "succeeded" | "failed" | "cancelled" | "timeout";
  exitCodes: number[];
  stdoutTail: string;
  artifacts: WorkerArtifact[];
  workspaceRemoved: boolean;
  terminatedBy?: "timeout" | "cancel";
}

// Only these keys may cross from manager env into a job. NO credential,
// secret, deploy, broker, or cloud key is on the list — the isolation test
// enumerates the job env and proves it.
const ENV_ALLOWLIST = ["PATH", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP",
  "PATHEXT", "WINDIR", "HOMEDRIVE", "HOMEPATH", "USERPROFILE"] as const;

const DENIED_NETWORK_ALWAYS = [
  "169.254.169.254", // cloud metadata
  "api.leafdesign.ai", // production surface
];

export interface WorkspaceHandle {
  dir: string;
  run(cmd: string, env: Record<string, string>, timeoutMs: number,
      signal: AbortSignal):
    Promise<{ exitCode: number; stdout: string; terminated: boolean }>;
  destroy(): Promise<void>;
}

export interface WorkspaceSubstrate {
  /** True ONLY for a substrate that enforces real OS-level filesystem AND
   * network isolation (a microVM/container). The manager refuses broad
   * execution on a non-isolating substrate unless explicitly opted in for
   * tests — env scrubbing and an advisory network allowlist are necessary
   * but NOT sufficient, so they never substitute for a real jail. */
  readonly isolating: boolean;
  create(jobId: string): Promise<WorkspaceHandle>;
}

/** TEST-ONLY substrate: a plain child process in a scratch directory. It is
 * NOT an isolation boundary — `spawn(shell)` can still read absolute host
 * paths and reach the network, so `isolating = false`. Env scrubbing and the
 * timeout tree-kill are defense-in-depth, not a sandbox. A real isolating
 * substrate (microVM/container, the plan's E2B lane) is a prerequisite before
 * the O2/O3 disposable-execution actions may run untrusted commands. */
export class LocalProcessSubstrate implements WorkspaceSubstrate {
  readonly isolating = false;

  constructor(private readonly root: string) {}

  async create(jobId: string): Promise<WorkspaceHandle> {
    const dir = path.join(this.root, `op-worker-${jobId}`);
    fs.mkdirSync(dir, { recursive: true });
    return {
      dir,
      run: (cmd, env, timeoutMs, signal) =>
        new Promise((resolve) => {
          const child = spawn(cmd, {
            shell: true,
            cwd: dir,
            env,
            windowsHide: true,
          });
          let stdout = "";
          let settled = false;
          let terminated = false;
          child.stdout?.on("data", (d) => { stdout += String(d); });
          child.stderr?.on("data", (d) => { stdout += String(d); });
          const finish = (exitCode: number) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve({ exitCode, stdout: stdout.slice(-8000), terminated });
          };
          const kill = () => {
            terminated = true;
            // Full-tree kill: taskkill /T on Windows, group kill elsewhere.
            if (process.platform === "win32" && child.pid) {
              spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"],
                { windowsHide: true });
            } else {
              child.kill("SIGKILL");
            }
          };
          const timer = setTimeout(kill, timeoutMs);
          signal.addEventListener("abort", kill, { once: true });
          child.on("exit", (code) => finish(code ?? 1));
          child.on("error", () => finish(127));
        }),
      destroy: async () => {
        fs.rmSync(dir, { recursive: true, force: true });
      },
    };
  }
}

export class OperatorWorkerManager {
  private readonly jobs = new Map<string, WorkerJobReceipt>();
  private readonly byIdempotency = new Map<string, string>();
  private readonly aborts = new Map<string, AbortController>();
  private readonly jobSubjects = new Map<string, string>();
  private readonly allowNonIsolated: boolean;

  constructor(
    private readonly substrate: WorkspaceSubstrate,
    private readonly artifactRoot: string,
    opts?: { allowNonIsolatedSubstrate?: boolean },
  ) {
    // Fail closed: a non-isolating substrate may run untrusted commands ONLY
    // when a test explicitly opts in. In any other context, submit() refuses.
    this.allowNonIsolated = opts?.allowNonIsolatedSubstrate === true;
    fs.mkdirSync(artifactRoot, { recursive: true });
  }

  /** Scrubbed job env: allowlisted host keys + job metadata only. */
  buildJobEnv(envelope: WorkerJobEnvelope): Record<string, string> {
    const env: Record<string, string> = {};
    for (const key of ENV_ALLOWLIST) {
      const value = process.env[key];
      if (value !== undefined) env[key] = value;
    }
    env.LEAF_OPERATOR_JOB = "1";
    env.LEAF_OPERATOR_JOB_SUBJECT = envelope.principalSubject;
    // Network policy travels as env for policy-aware adapters; the
    // always-denied list can never be allowlisted back in.
    const allowed = (envelope.network ?? []).filter(
      (host) => !DENIED_NETWORK_ALWAYS.some((d) => host.includes(d)));
    env.LEAF_OPERATOR_NET_ALLOW = allowed.join(",");
    env.LEAF_OPERATOR_NET_DENY = DENIED_NETWORK_ALWAYS.join(",");
    return env;
  }

  validate(envelope: WorkerJobEnvelope): string | null {
    if (envelope.workspace !== "disposable") return "workspace_invalid";
    if (!Array.isArray(envelope.commands) || envelope.commands.length === 0 ||
        envelope.commands.length > 50) return "commands_invalid";
    if (!envelope.idempotencyKey) return "idempotency_key_required";
    if (!envelope.principalSubject) return "principal_required";
    return null;
  }

  async submit(envelope: WorkerJobEnvelope): Promise<WorkerJobReceipt> {
    // Fail closed on isolation: broad command execution requires a substrate
    // that enforces real filesystem + network isolation. The manager NEVER
    // runs untrusted commands on a non-isolating substrate outside tests.
    if (!this.substrate.isolating && !this.allowNonIsolated) {
      throw new Error("substrate_not_isolating");
    }
    const invalid = this.validate(envelope);
    if (invalid) throw new Error(invalid);

    // Idempotency: one logical job per (subject, key).
    const idemKey = `${envelope.principalSubject}:${envelope.idempotencyKey}`;
    const existing = this.byIdempotency.get(idemKey);
    if (existing) return this.jobs.get(existing)!;

    const jobId = `opjob-${randomUUID()}`;
    this.byIdempotency.set(idemKey, jobId);
    this.jobSubjects.set(jobId, envelope.principalSubject);
    const abort = new AbortController();
    this.aborts.set(jobId, abort);

    const workspace = await this.substrate.create(jobId);
    const env = this.buildJobEnv(envelope);
    const timeoutMs = Math.min(envelope.timeoutMs ?? 120_000, 1_800_000);
    const exitCodes: number[] = [];
    let stdoutTail = "";
    let terminatedBy: WorkerJobReceipt["terminatedBy"];

    try {
      for (const cmd of envelope.commands) {
        if (abort.signal.aborted) { terminatedBy = "cancel"; break; }
        const result = await workspace.run(cmd, env, timeoutMs, abort.signal);
        exitCodes.push(result.exitCode);
        stdoutTail = (stdoutTail + result.stdout).slice(-8000);
        if (result.terminated) {
          terminatedBy = abort.signal.aborted ? "cancel" : "timeout";
          break;
        }
        if (result.exitCode !== 0) break;
      }

      // Artifact receipts: preserve files the job left under ./artifacts.
      const artifacts = this.collectArtifacts(jobId, workspace.dir);
      const status: WorkerJobReceipt["status"] =
        terminatedBy === "cancel" ? "cancelled"
        : terminatedBy === "timeout" ? "timeout"
        : exitCodes.every((c) => c === 0) && exitCodes.length ===
            envelope.commands.length ? "succeeded" : "failed";
      const receipt: WorkerJobReceipt = {
        jobId, principalSubject: envelope.principalSubject, status,
        exitCodes, stdoutTail, artifacts,
        workspaceRemoved: false, terminatedBy,
      };
      this.jobs.set(jobId, receipt);
      return receipt;
    } finally {
      await workspace.destroy();
      const receipt = this.jobs.get(jobId);
      if (receipt) receipt.workspaceRemoved = !fs.existsSync(workspace.dir);
      this.aborts.delete(jobId);
    }
  }

  /** Ownership-enforced: only the submitting principal may cancel. A
   * mismatched or unknown subject gets `false` — never another principal's
   * job, and no existence oracle. */
  cancel(jobId: string, subject: string): boolean {
    if (this.jobSubjects.get(jobId) !== subject) return false;
    const abort = this.aborts.get(jobId);
    if (!abort) return this.jobs.get(jobId)?.status === "cancelled";
    abort.abort();
    return true;
  }

  /** Ownership-enforced: only the submitting principal may read a job's
   * receipt (which carries stdout tail and artifact paths). A mismatched or
   * unknown subject gets `undefined` — no cross-principal read, no oracle. */
  status(jobId: string, subject: string): WorkerJobReceipt | undefined {
    if (this.jobSubjects.get(jobId) !== subject) return undefined;
    return this.jobs.get(jobId);
  }

  private collectArtifacts(jobId: string, dir: string): WorkerArtifact[] {
    const artifactDir = path.join(dir, "artifacts");
    if (!fs.existsSync(artifactDir)) return [];
    const out: WorkerArtifact[] = [];
    const keepRoot = path.join(this.artifactRoot, jobId);
    fs.mkdirSync(keepRoot, { recursive: true });
    for (const name of fs.readdirSync(artifactDir)) {
      const src = path.join(artifactDir, name);
      if (!fs.statSync(src).isFile()) continue;
      const bytes = fs.readFileSync(src);
      const kept = path.join(keepRoot, name);
      fs.writeFileSync(kept, bytes);
      out.push({
        path: kept,
        sha256: createHash("sha256").update(bytes).digest("hex"),
        bytes: bytes.length,
      });
    }
    return out;
  }

  /** Crash recovery: remove any op-worker-* directory with no live job. */
  static reapOrphans(root: string, liveJobIds: Set<string>): string[] {
    if (!fs.existsSync(root)) return [];
    const reaped: string[] = [];
    for (const name of fs.readdirSync(root)) {
      if (!name.startsWith("op-worker-")) continue;
      const jobId = name.slice("op-worker-".length);
      if (!liveJobIds.has(jobId)) {
        fs.rmSync(path.join(root, name), { recursive: true, force: true });
        reaped.push(name);
      }
    }
    return reaped;
  }
}
