/**
 * `fsTenantRepo` author tool - read/write STRICTLY scoped to the tenant checkout
 * dir. Any path that escapes the checkout root is rejected (mirrors hot-script
 * SPEC section 10: the session gets a repo-scoped filesystem and nothing else).
 *
 * Both the fake AgentRunner and the real Agent SDK runner are handed one of these
 * (bound to the checkout dir) as the ONLY filesystem the session can touch.
 */

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { FsTenantRepoTool } from "../../ports/index.js";

export class FsTenantRepo implements FsTenantRepoTool {
  readonly root: string;

  constructor(rootDir: string) {
    this.root = resolve(rootDir);
  }

  /** Resolve a caller-supplied relative path and hard-reject any escape. */
  private safe(relPath: string): string {
    if (isAbsolute(relPath)) {
      throw new Error(`fsTenantRepo: absolute paths are not allowed (${relPath})`);
    }
    const abs = resolve(this.root, relPath);
    const rel = relative(this.root, abs);
    if (rel === ".." || rel.startsWith(".." + sep) || isAbsolute(rel)) {
      throw new Error(`fsTenantRepo: path escapes the tenant repo (${relPath})`);
    }
    return abs;
  }

  readFile(relPath: string): string {
    return readFileSync(this.safe(relPath), "utf8");
  }

  writeFile(relPath: string, content: string): void {
    const abs = this.safe(relPath);
    mkdirSync(dirname(abs), { recursive: true });
    writeFileSync(abs, content, "utf8");
  }

  exists(relPath: string): boolean {
    return existsSync(this.safe(relPath));
  }

  listDir(relPath = "."): string[] {
    return readdirSync(this.safe(relPath));
  }
}
