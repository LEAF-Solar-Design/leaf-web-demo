/**
 * Fake RepoEditor — deterministic, no network, no auth. Interprets the
 * instruction as newline-separated commands so hermetic tests and CI can
 * exercise the in-app edit lane end to end:
 *
 *   write <path>: <content>     create/overwrite (content to end of line)
 *   append <path>: <content>    append a line
 *   delete <path>               remove a file
 *
 * Anything else falls back to writing the instruction into .mushy-note.md —
 * the fake never refuses, mirroring the live editor's do-what-was-asked role.
 * Containment is the SAME safeRepoRelative rule the live editor enforces.
 */

import { appendFileSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import type { RepoEditInput, RepoEditResult, RepoEditor } from "../impl/repoEditRunner.js";
import { applyRepoEdit, safeRepoRelative } from "../impl/repoEditRunner.js";

export class FakeRepoEditor implements RepoEditor {
  /** Number of edit() calls (a shell test can assert the lane was exercised). */
  calls = 0;

  async edit(input: RepoEditInput): Promise<RepoEditResult> {
    if (input.signal?.aborted) throw new Error("in-app edit session was cancelled");
    this.calls += 1;
    const changed = new Set<string>();
    const acts: string[] = [];
    let attemptedWrites = false;
    const lines = input.instruction.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    let commanded = false;
    for (const line of lines) {
      // A read-only reply: no writes, summary = the text. Lets hermetic tests
      // drive the ANSWERED outcome the same way live Claude answers a
      // question about the repo without editing anything.
      let m = line.match(/^answer\s+([\s\S]+)$/);
      if (m) {
        commanded = true;
        acts.push(m[1]!);
        continue;
      }
      m = line.match(/^wait\s+(\d+)$/);
      if (m) {
        const delayMs = Math.min(5000, Number.parseInt(m[1], 10));
        await new Promise((resolve) => setTimeout(resolve, delayMs));
        if (input.signal?.aborted) throw new Error("in-app edit session was cancelled");
        continue;
      }
      m = line.match(/^write\s+(\S+):\s?([\s\S]*)$/);
      if (m) {
        commanded = true;
        attemptedWrites = true;
        const abs = safeRepoRelative(input.repoDir, m[1]);
        if (!abs) throw new Error(`fake editor: path escapes the repo: ${m[1]}`);
        mkdirSync(dirname(abs), { recursive: true });
        writeFileSync(abs, m[2] + "\n", "utf8");
        changed.add(m[1]);
        acts.push(`wrote ${m[1]}`);
        continue;
      }
      m = line.match(/^append\s+(\S+):\s?([\s\S]*)$/);
      if (m) {
        commanded = true;
        attemptedWrites = true;
        const abs = safeRepoRelative(input.repoDir, m[1]);
        if (!abs) throw new Error(`fake editor: path escapes the repo: ${m[1]}`);
        appendFileSync(abs, m[2] + "\n", "utf8");
        changed.add(m[1]);
        acts.push(`appended to ${m[1]}`);
        continue;
      }
      m = line.match(/^replace\s+(\S+):\s?([\s\S]*?)\s=>\s([\s\S]*)$/);
      if (m) {
        commanded = true;
        attemptedWrites = true;
        const result = applyRepoEdit(input.repoDir, m[1]!, m[2]!, m[3]!);
        if (result.changed) {
          changed.add(m[1]!);
          acts.push(`replaced in ${m[1]}`);
        } else {
          acts.push(`no change to ${m[1]}`);
        }
        continue;
      }
      m = line.match(/^delete\s+(\S+)$/);
      if (m) {
        commanded = true;
        attemptedWrites = true;
        const abs = safeRepoRelative(input.repoDir, m[1]);
        if (!abs) throw new Error(`fake editor: path escapes the repo: ${m[1]}`);
        rmSync(abs);
        changed.add(m[1]);
        acts.push(`deleted ${m[1]}`);
      }
    }
    if (!commanded) {
      attemptedWrites = true;
      const abs = safeRepoRelative(input.repoDir, ".mushy-note.md");
      if (!abs) throw new Error("fake editor: repo dir unusable");
      writeFileSync(abs, `# fake edit\n\n${input.instruction}\n`, "utf8");
      changed.add(".mushy-note.md");
      acts.push("wrote .mushy-note.md (fake fallback: no model attached)");
    }
    return { summary: acts.join("; "), changedFiles: [...changed].sort(), attemptedWrites };
  }
}
