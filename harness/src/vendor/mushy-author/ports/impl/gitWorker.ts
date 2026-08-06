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
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { scrubSecrets } from "./envScrub.js";

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

/**
 * Locate scripts/git-worker.cjs relative to THIS module. The module can run
 * from the package itself (src/ or dist/src/), or vendored inside a consumer
 * tree at any depth (e.g. harness/src/vendor/mushy-author/ports/impl), so a
 * fixed levels-up count cannot work everywhere: walk upward and take the first
 * scripts/git-worker.cjs found. Callers that keep the worker script somewhere
 * else entirely pass an explicit path instead.
 */
function defaultWorkerScript(): string | null {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let hop = 0; hop < 12; hop++) {
    const candidate = join(dir, "scripts", "git-worker.cjs");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return null;
}

export function startGitWorker(scriptPath?: string): boolean {
  if (worker) return true;
  const script = scriptPath ?? defaultWorkerScript();
  if (!script) return false;
  try {
    // Scrubbed env (sol-critic R5): the worker — and every git.exe it spawns,
    // which inherits the worker's env — must never hold a credential or hop
    // secret. Git needs PATH/HOME/system vars only, all of which survive the scrub.
    const child = spawn(process.execPath, [script], {
      stdio: ["pipe", "pipe", "inherit"],
      windowsHide: true,
      env: scrubSecrets(process.env),
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

export function stopGitWorker(): void {
  const active = worker;
  worker = null;
  for (const [id, request] of pending) {
    pending.delete(id);
    request.resolve({ ok: false, error: "git worker stopped" });
  }
  if (!active) return;
  try {
    active.kill();
  } catch {
    /* process exit remains the final cleanup boundary */
  }
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
