/**
 * `fsTenantRepo` author tool - read/write STRICTLY scoped to the tenant checkout
 * dir. Any path that escapes the checkout root is rejected (mirrors hot-script
 * SPEC section 10: the session gets a repo-scoped filesystem and nothing else).
 *
 * Both the fake AgentRunner and the real Agent SDK runner are handed one of these
 * (bound to the checkout dir) as the ONLY filesystem the session can touch.
 */

import {
  closeSync,
  existsSync,
  fstatSync,
  ftruncateSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  writeSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type { FsTenantRepoTool } from "../../ports/index.js";
import {
  assertOpenedFileIdentity,
  isWindowsUnsafeRepoComponent,
  resolveContainedRepoPath,
  revalidateResolvedRepoPath,
  type ResolvedRepoPath,
} from "./repoPathSafety.js";

export class FsTenantRepo implements FsTenantRepoTool {
  readonly root: string;

  constructor(rootDir: string) {
    // Realpath so containment compares against the true directory even when the
    // configured root itself arrives through a symlink (macOS /tmp and friends).
    const abs = resolve(rootDir);
    this.root = existsSync(abs) ? realpathSync(abs) : abs;
  }

  /** Resolve a caller-supplied relative path and hard-reject any escape. */
  private safe(relPath: string): ResolvedRepoPath {
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
    if (rel.replaceAll("\\", "/").split("/")
      .some((part) => part !== "." && isWindowsUnsafeRepoComponent(part))) {
      throw new Error(`fsTenantRepo: unsafe Windows names are off-limits (${relPath})`);
    }
    // The lexical checks above are spoofable by a link committed in the checkout
    // (sol-critic round 3: `alias -> .git` lets `alias/config` reach `.git/config`,
    // and a link targeting outside the root escapes entirely) — writeFileSync
    // follows links. Links only enter via the checkout itself (this tool writes
    // regular files), and the author session has no legitimate use for traversing
    // one, so reject any symlink/junction component outright.
    const target = resolveContainedRepoPath(this.root, relPath);
    if (!target) throw new Error(`fsTenantRepo: linked or unsafe paths are off-limits (${relPath})`);
    return revalidateResolvedRepoPath(target);
  }

  readFile(relPath: string): string {
    const target = this.safe(relPath);
    const fd = openSync(target.path, "r");
    try {
      assertOpenedFileIdentity(target, fstatSync(fd));
      return readFileSync(fd, "utf8");
    } finally {
      closeSync(fd);
    }
  }

  writeFile(relPath: string, content: string): void {
    let target = this.safe(relPath);
    mkdirSync(dirname(target.path), { recursive: true });
    target = this.safe(relPath);
    const final = target.components.at(-1);
    const flags = final?.path === target.path ? "r+" : "wx";
    const fd = openSync(target.path, flags);
    try {
      const stat = fstatSync(fd);
      if (flags === "r+") assertOpenedFileIdentity(target, stat);
      else if (!stat.isFile() || stat.nlink !== 1) {
        throw new Error("fsTenantRepo: repository file identity changed before operation");
      }
      const bytes = Buffer.from(content, "utf8");
      if (flags === "r+") ftruncateSync(fd, 0);
      writeSync(fd, bytes, 0, bytes.length, 0);
    } finally {
      closeSync(fd);
    }
  }

  exists(relPath: string): boolean {
    const target = this.safe(relPath);
    revalidateResolvedRepoPath(target);
    return existsSync(target.path);
  }

  listDir(relPath = "."): string[] {
    const target = this.safe(relPath);
    revalidateResolvedRepoPath(target);
    return readdirSync(target.path).filter((name) => !isWindowsUnsafeRepoComponent(name));
  }
}
