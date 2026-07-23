/**
 * `fsTenantRepo` author tool - read/write STRICTLY scoped to the tenant checkout
 * dir. Any path that escapes the checkout root is rejected (mirrors hot-script
 * SPEC section 10: the session gets a repo-scoped filesystem and nothing else).
 *
 * Both the fake AgentRunner and the real Agent SDK runner are handed one of these
 * (bound to the checkout dir) as the ONLY filesystem the session can touch.
 */

import {
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  realpathSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { FsTenantRepoTool } from "../../ports/index.js";

export class FsTenantRepo implements FsTenantRepoTool {
  readonly root: string;

  constructor(rootDir: string) {
    // Realpath so containment compares against the true directory even when the
    // configured root itself arrives through a symlink (macOS /tmp and friends).
    const abs = resolve(rootDir);
    this.root = existsSync(abs) ? realpathSync(abs) : abs;
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
    // The .git directory is OFF-LIMITS to the author session (sol-critic R5): a
    // writable .git/config or .git/hooks would let model-authored repo content
    // install filters/hooks that run during the register commit with repo access.
    // Reads are blocked too — the session has no business inside git internals.
    if (rel.split(sep).some((seg) => seg.toLowerCase() === ".git")) {
      throw new Error(`fsTenantRepo: the .git directory is off-limits (${relPath})`);
    }
    // The lexical checks above are spoofable by a link committed in the checkout
    // (sol-critic round 3: `alias -> .git` lets `alias/config` reach `.git/config`,
    // and a link targeting outside the root escapes entirely) — writeFileSync
    // follows links. Links only enter via the checkout itself (this tool writes
    // regular files), and the author session has no legitimate use for traversing
    // one, so reject any symlink/junction component outright.
    let probe = this.root;
    for (const seg of rel.split(sep)) {
      if (seg === "") continue;
      probe = resolve(probe, seg);
      let stat;
      try {
        stat = lstatSync(probe);
      } catch {
        break; // deepest existing ancestor passed; missing components can't be links
      }
      if (stat.isSymbolicLink()) {
        throw new Error(`fsTenantRepo: symlinked paths are off-limits (${relPath})`);
      }
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
