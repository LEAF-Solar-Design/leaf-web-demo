/**
 * Curated skill-bundle attachment for the converse lane.
 *
 * PROVEN MECHANISM (Wave-0 spike, 2026-07-29 — see
 * C:/tmp/leaf-chat-cc-parity/SKILLS-SPIKE-FINDINGS.md):
 *
 *     settingSources: []                                   // no ~/.claude
 *     plugins: [{ type: 'local', path: BUNDLE, skipMcpDiscovery: true }]
 *     skills:  ['name', ...]                               // EXPLICIT allowlist
 *
 * Four things this module encodes, each learned rather than assumed:
 *
 * 1. `settingSources` CANNOT point at a curated directory — its type is
 *    'user' | 'project' | 'local', and 'user' IS ~/.claude. It stays `[]`;
 *    `plugins` supplies skills from an arbitrary path.
 *
 * 2. `skills: 'all'` also enables Claude Code's BUNDLED skills (run, review,
 *    security-review, …) — developer tooling with no business in a tenant
 *    session. This module always emits an explicit list, never 'all'.
 *
 * 3. `skills` is a CONTEXT FILTER, NOT A SANDBOX: unlisted skills stay on disk
 *    and are reachable via Read/Bash. So the tenant/operator split is enforced
 *    by WHICH BUNDLE IS MOUNTED, never by filtering one superset.
 *
 * 4. …and because (3) makes the mounted path the whole security boundary, a
 *    path alone is not enough. A bundle DECLARES its tier, the process
 *    DECLARES the tier it serves, and a mismatch FAILS CLOSED. Pointing a
 *    tenant-tier harness at the operator bundle therefore mounts nothing
 *    instead of silently handing tenants fleet-ops skills (review finding).
 *    Deriving the tier per-turn from the authenticated tenant would be
 *    stronger still, but ConverseTurnInput is a frozen shape with no tier
 *    field; that is the documented follow-up.
 *
 * OFF unless configured. No bundle, no attachment, and the session runs
 * exactly as it does today.
 */
import { closeSync, existsSync, opendirSync, openSync, readSync, readdirSync, realpathSync, statSync } from "node:fs";
import { join, resolve, sep } from "node:path";

export type SkillTier = "tenant-safe" | "operator";

export type SkillBundleAttachment = {
  /** SDK `plugins` entry supplying the bundle's skills. */
  plugin: { type: "local"; path: string; skipMcpDiscovery: true };
  /** SDK `skills` allowlist — always explicit, never 'all'. */
  skills: string[];
  /** The tier this bundle declared, echoed for logging/telemetry. */
  tier: SkillTier;
};

/** A skill discovered in a bundle, for the registry/UI to list. */
export type BundledSkill = { name: string; description: string };

const SKILLS_DIR = "skills";
const MANIFEST = join(".claude-plugin", "plugin.json");
// Frontmatter keys that make a skill EXECUTABLE or spawn something, rather
// than instruct. Refused outright at the artifact boundary — the runtime flags
// (disableSkillShellExecution, disableAllHooks) do not cover all of these:
// `context: fork` spawns a SUBAGENT and is disabled by neither (review round
// 3). A curated skill is prose; anything here means the bundle is wrong.
const EXECUTABLE_FRONTMATTER =
  /^\s*(hooks|allowed-tools|allowedtools|context|agent|agents|background|monitor|monitors|command|commands|mcp|mcpServers)\s*:/i;

const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---/;

/**
 * A skill name must be a plain slug.
 *
 * These values are handed to the SDK's `skills` allowlist, which expands them
 * into comma-delimited tool patterns — so a name like `safe),Bash` is not
 * cosmetic, it can widen the allowlist the money-gate depends on. `../../x`
 * likewise has no business resolving anywhere. Anything that is not a simple
 * slug is refused rather than sanitized: quietly rewriting a hostile name
 * keeps a malicious bundle half-working, and refusing is unambiguous.
 */
export const SKILL_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/i;

/** Windows reserves these device names; a directory called CON or NUL is a
 * portability trap, not a skill. Checked case-insensitively, before any
 * extension. */
const RESERVED_NAMES = new Set([
  "con", "prn", "aux", "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);

/** Skill docs are prose; anything larger is not a skill and must not be read
 * into memory on a per-turn path. */
export const MAX_SKILL_FILE_BYTES = 256 * 1024;
/** A bundle with more entries than this is misconfigured, not curated. */
export const MAX_SKILLS = 250;
/** A plugin manifest is a few keys; anything larger is not one. */
export const MAX_MANIFEST_BYTES = 64 * 1024;

export function isValidSkillName(name: unknown): name is string {
  if (typeof name !== "string" || !SKILL_NAME_RE.test(name)) return false;
  // A trailing dot is silently stripped by Windows, so `x.` and `x` would name
  // the same directory while reading as two distinct allowlist entries.
  if (name.endsWith(".")) return false;
  const base = name.split(".")[0].toLowerCase();
  return !RESERVED_NAMES.has(base);
}

/**
 * Read at most `MAX_SKILL_FILE_BYTES` from a file, in ONE open.
 *
 * Replaces a stat-then-read pair: between those two calls the file can be
 * swapped for a huge one (TOCTOU), leaving the size check decided against a
 * file that is no longer there. A bounded read from a single descriptor cannot
 * be beaten that way. An oversized file is simply TRUNCATED at the cap: since
 * frontmatter sits at the top, a long body still parses and is a legitimate
 * skill. Only frontmatter pushed BEYOND the cap loses its closing `---`, and
 * that one fails to parse and is skipped.
 */
function readCapped(path: string, limit: number = MAX_SKILL_FILE_BYTES): string | null {
  let fd: number | null = null;
  try {
    fd = openSync(path, "r");
    const buf = Buffer.allocUnsafe(limit);
    const read = readSync(fd, buf, 0, limit, 0);
    return buf.subarray(0, read).toString("utf8");
  } catch {
    return null;
  } finally {
    if (fd !== null) { try { closeSync(fd); } catch { /* already closed */ } }
  }
}

/**
 * A curated bundle's files are its own. More than one directory entry pointing
 * at the same inode means the file also lives somewhere else, which is exactly
 * how a hardlinked operator SKILL.md would sit inside a tenant-safe wrapper:
 * real-path containment cannot see it, because a hardlink HAS no separate path.
 *
 * Worth stating plainly: an attacker who can write into the bundle could just
 * copy the operator skill's text in, so this is not the last line of defence —
 * the bundle being a read-only build artifact is. What this does catch is the
 * realistic version: a build pipeline that links instead of copying, silently
 * exposing another tier's inode.
 */
function isMultiplyLinked(path: string): boolean {
  try {
    return statSync(path).nlink > 1;
  } catch {
    return true; // cannot prove it is exclusive -> refuse
  }
}

/** Resolve `child` and confirm it stays inside `rootReal`.
 *
 * THE containment check. statSync/readFileSync follow symlinks, so a
 * tenant-safe bundle could otherwise hold `skills/x -> ~/.claude/skills/x` and
 * pull an operator skill into the allowlist. The mounted directory is the whole
 * security boundary, so it has to be a REAL one: comparing resolved real paths
 * covers symlinks, junctions and `..` in a single check.
 */
function containedRealPath(rootReal: string, child: string): string | null {
  try {
    const real = realpathSync(child);
    if (real === rootReal) return real;
    const prefix = rootReal.endsWith(sep) ? rootReal : rootReal + sep;
    return real.startsWith(prefix) ? real : null;
  } catch {
    return null;
  }
}

/** Pull `name` / `description` out of a SKILL.md's YAML frontmatter.
 *
 * Deliberately a line scan, not a YAML parser: the two fields are flat scalars
 * and a YAML dependency for a two-key read would be a new transitive tree (and
 * CVE surface) for nothing. FIRST occurrence of each key wins, so a duplicate
 * key cannot override the name that was validated.
 */
export function parseSkillFrontmatter(source: string): BundledSkill | null {
  const match = FRONTMATTER.exec(source ?? "");
  if (!match) return null;
  let name: string | null = null;
  let description: string | null = null;
  for (const line of match[1].split(/\r?\n/)) {
    // A curated skill is INSTRUCTIONS. Frontmatter declaring executable
    // machinery — hooks, or its own tool allowlist — is REFUSED rather than
    // ignored. disableAllHooks/disableSkillShellExecution already stop these at
    // the SDK, so this is defence in depth at the artifact boundary: the bundle
    // we build should not CONTAIN a skill that wants to execute, and silently
    // accepting one would rest the whole guarantee on a single runtime flag
    // (review round 2).
    if (EXECUTABLE_FRONTMATTER.test(line)) return null;
    const kv = /^(name|description)\s*:\s*(.*)$/.exec(line.trim());
    if (!kv) continue;
    const value = kv[2].trim().replace(/^["']|["']$/g, "");
    if (kv[1] === "name") { if (name === null) name = value; }
    else if (description === null) description = value;
  }
  if (!isValidSkillName(name)) return null;
  return { name, description: description ?? "" };
}

/** The tier a bundle declares in `.claude-plugin/plugin.json` (`leafTier`). */
export function readBundleTier(bundlePath: string): SkillTier | null {
  try {
    const rootReal = realpathSync(bundlePath);
    // The manifest DECLARES the tier, so a symlinked manifest pointing at an
    // operator bundle's JSON would let a wrapper claim "tenant-safe".
    const manifestReal = containedRealPath(rootReal, join(bundlePath, MANIFEST));
    if (!manifestReal) return null;
    if (isMultiplyLinked(manifestReal)) return null;
    // Bounded like every other read on this path: a hostile manifest must not
    // be able to allocate arbitrarily before JSON.parse ever sees it.
    const raw = readCapped(manifestReal, MAX_MANIFEST_BYTES);
    if (raw === null) return null;
    const tier = (JSON.parse(raw) as { leafTier?: unknown }).leafTier;
    return tier === "tenant-safe" || tier === "operator" ? tier : null;
  } catch {
    return null;
  }
}

/**
 * Discover the skills a bundle actually contains.
 *
 * The allowlist is built from what is ON DISK rather than a hand-maintained
 * list: a listed-but-absent skill would be silently unavailable, and a present
 * but unlisted one invisible. Reading the directory keeps them in step.
 */
export function discoverSkills(bundlePath: string): BundledSkill[] {
  let rootReal: string;
  try {
    rootReal = realpathSync(resolve(bundlePath));
  } catch {
    return [];
  }
  const skillsRoot = containedRealPath(rootReal, join(bundlePath, SKILLS_DIR));
  if (!skillsRoot) return [];

  const found: BundledSkill[] = [];
  const seen = new Set<string>();
  // STREAM the directory rather than readdirSync: materialising every entry of
  // a directory with an extreme entry count allocates before any cap applies.
  // opendirSync hands them back one at a time, so the bound below is real.
  let dir;
  try {
    dir = opendirSync(skillsRoot);
  } catch {
    return [];
  }
  let examined = 0;
  try {
    for (;;) {
      if (found.length >= MAX_SKILLS || examined >= MAX_SKILLS * 2) break;
      const dirent = dir.readSync();
      if (dirent === null) break;
      const entry = dirent.name;
      examined += 1;
      // Reject the directory name too: it is what the SDK resolves plugin-
      // qualified skills by, so `..` must never get as far as a read.
      if (!isValidSkillName(entry)) continue;
      // Case-only collisions (`Probe` vs `probe`) are one directory on Windows
      // and two elsewhere; either way two allowlist entries differing only by
      // case are ambiguous, so the first wins.
      const key = entry.toLowerCase();
      if (seen.has(key)) continue;

      // Containment, not existence: the real path of BOTH the directory and
      // its SKILL.md must live inside the bundle.
      const dirReal = containedRealPath(skillsRoot, join(skillsRoot, entry));
      if (!dirReal) continue;
      const fileReal = containedRealPath(skillsRoot, join(skillsRoot, entry, "SKILL.md"));
      if (!fileReal) continue;
      // ...and the file must not ALSO live outside the bundle as a hardlink,
      // which no path check can see.
      if (isMultiplyLinked(fileReal)) continue;
      try {
        if (!statSync(dirReal).isDirectory()) continue;
        const source = readCapped(fileReal);
        if (source === null) continue;
        const parsed = parseSkillFrontmatter(source);
        // The frontmatter name must match its directory: a doc claiming a
        // different name than the folder it lives in is ambiguous to resolve.
        if (parsed && parsed.name === entry) {
          seen.add(key);
          found.push(parsed);
        }
      } catch {
        // An unreadable skill is skipped, never fatal: one bad file must not
        // take down every other skill in the bundle.
        continue;
      }
    }
  } finally {
    try { dir.closeSync(); } catch { /* already closed */ }
  }
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Build the SDK attachment for this process's tier, or null when unconfigured
 * or mismatched.
 *
 * `LEAF_SKILLS_TIER` is what this harness process is allowed to serve;
 * `LEAF_SKILLS_BUNDLE_PATH` is where the bundle lives. Both must be present
 * AND the bundle must declare the same tier. That is the fail-closed check:
 * a tenant-tier process pointed at the operator bundle mounts NOTHING.
 */
/** The only entries a curated bundle may contain at its root, and under
 * skills/. Anything else means the artifact was not produced by the verified
 * pipeline, and mounting it would hand the SDK whatever else is in there. */
const ALLOWED_ROOT_ENTRIES = new Set([".claude-plugin", "skills"]);

export function hasStrictBundleShape(bundlePath: string): boolean {
  try {
    for (const entry of readdirSync(bundlePath, { withFileTypes: true })) {
      if (!ALLOWED_ROOT_ENTRIES.has(entry.name)) {
        console.error(
          `[leaf-skills] refusing to mount ${bundlePath}: unexpected entry ` +
          `${entry.name} at the bundle root`);
        return false;
      }
      if (!entry.isDirectory()) return false;
    }
    const manifestDir = join(bundlePath, ".claude-plugin");
    for (const entry of readdirSync(manifestDir, { withFileTypes: true })) {
      if (entry.name !== "plugin.json" || !entry.isFile()) return false;
    }
    const skillsDir = join(bundlePath, SKILLS_DIR);
    if (existsSync(skillsDir)) {
      for (const entry of readdirSync(skillsDir, { withFileTypes: true })) {
        // Directories only: a stray file under skills/ is not a skill and has
        // no business being mounted.
        if (!entry.isDirectory()) return false;
      }
    }
    return true;
  } catch {
    return false;
  }
}


export function skillBundleAttachment(
  env: NodeJS.ProcessEnv = process.env,
): SkillBundleAttachment | null {
  const path = env.LEAF_SKILLS_BUNDLE_PATH?.trim();
  const declaredTier = env.LEAF_SKILLS_TIER?.trim();
  if (!path || !declaredTier) return null;
  if (declaredTier !== "tenant-safe" && declaredTier !== "operator") return null;
  if (!existsSync(path)) return null;
  try {
    if (!statSync(path).isDirectory()) return null;
  } catch {
    return null;
  }

  // STRICT TREE. The SDK mounts the whole DIRECTORY as a plugin, and
  // `skipMcpDiscovery` only suppresses MCP: it still loads skills, hooks,
  // agents, commands and plugin MONITORS (unsandboxed tasks armed at session
  // start, which disableAllHooks does not cover). Validating only the skills we
  // happen to DISCOVER is therefore not enough — anything else sitting in the
  // directory is mounted too. So the loader now enforces the same shape the
  // offline verifier does: exactly `.claude-plugin/` and `skills/`, nothing
  // else, and under skills/ only directories. A bundle with one valid skill
  // and a monitor beside it is refused (review round 3 BLOCKER).
  if (!hasStrictBundleShape(path)) return null;

  const bundleTier = readBundleTier(path);
  if (bundleTier === null || bundleTier !== declaredTier) {
    // Fail closed and say why: a silent no-op here looks identical to "no
    // skills configured", and this is the boundary that keeps operator skills
    // away from tenants.
    console.error(
      `[leaf-skills] refusing to mount ${path}: bundle tier ${String(bundleTier)} ` +
      `does not match this process's tier ${declaredTier}`,
    );
    return null;
  }

  const skills = discoverSkills(path).map((s) => s.name);
  // An empty bundle attaches nothing: enabling the Skill tool with no skills
  // behind it adds a capability the model can only fail with.
  if (skills.length === 0) return null;
  return {
    plugin: { type: "local", path, skipMcpDiscovery: true },
    skills,
    tier: bundleTier,
  };
}
