/**
 * Client for the pre-spawned git worker (scripts/git-worker.cjs).
 *
 * WHY: once this node process has hosted an Agent SDK `claude` process tree,
 * spawning git.exe can fail with 0xC0000142 (STATUS_DLL_INIT_FAILED) on
 * Windows. The worker is spawned at harness BOOT — before any SDK session —
 * so its spawn context stays clean for the whole process lifetime.
 *
 * startGitWorker() must be called at boot (serve.ts). When no worker is
 * running (hermetic tests, drive.ts), callers fall back to in-process git.
 */
import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

export interface GitCommitRequest {
  dir: string;
  message: string;
  name: string;
  email: string;
}

interface Pending {
  resolve: (v: { ok: boolean; commit?: string; error?: string }) => void;
}

let worker: ChildProcess | null = null;
let seq = 0;
const pending = new Map<number, Pending>();

export function startGitWorker(): boolean {
  if (worker) return true;
  const here = dirname(fileURLToPath(import.meta.url));
  // dist/src/ports/impl -> repo harness root is four levels up
  const script = join(here, "..", "..", "..", "..", "scripts", "git-worker.cjs");
  try {
    const child = spawn(process.execPath, [script], {
      stdio: ["pipe", "pipe", "inherit"],
      windowsHide: true,
    });
    const rl = createInterface({ input: child.stdout! });
    rl.on("line", (line) => {
      try {
        const msg = JSON.parse(line) as { id: number; ok: boolean; commit?: string; error?: string };
        const p = pending.get(msg.id);
        if (p) {
          pending.delete(msg.id);
          p.resolve(msg);
        }
      } catch {
        /* ignore malformed worker output */
      }
    });
    child.on("exit", () => {
      worker = null;
      for (const [id, p] of pending) {
        pending.delete(id);
        p.resolve({ ok: false, error: "git worker exited" });
      }
    });
    worker = child;
    return true;
  } catch {
    worker = null;
    return false;
  }
}

export function gitWorkerAvailable(): boolean {
  return worker !== null;
}

export function workerCommit(req: GitCommitRequest, timeoutMs = 60_000): Promise<{ ok: boolean; commit?: string; error?: string }> {
  if (!worker || !worker.stdin) return Promise.resolve({ ok: false, error: "no git worker" });
  const id = ++seq;
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      resolve({ ok: false, error: "git worker timeout" });
    }, timeoutMs);
    pending.set(id, {
      resolve: (v) => {
        clearTimeout(timer);
        resolve(v);
      },
    });
    worker!.stdin!.write(JSON.stringify({ id, ...req }) + "\n");
  });
}
