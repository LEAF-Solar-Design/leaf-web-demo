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
import { readdirSync, readFileSync, existsSync, statSync } from "node:fs";
import { join } from "node:path";

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

/** Skill docs are prose; anything larger is not a skill and must not be read
 * into memory on a per-turn path. */
export const MAX_SKILL_FILE_BYTES = 256 * 1024;
/** A bundle with more entries than this is misconfigured, not curated. */
export const MAX_SKILLS = 250;

export function isValidSkillName(name: unknown): name is string {
  return typeof name === "string" && SKILL_NAME_RE.test(name);
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
    const raw = readFileSync(join(bundlePath, MANIFEST), "utf8");
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
  const root = join(bundlePath, SKILLS_DIR);
  if (!existsSync(root)) return [];
  const found: BundledSkill[] = [];
  let entries: string[];
  try {
    entries = readdirSync(root);
  } catch {
    return [];
  }
  for (const entry of entries) {
    if (found.length >= MAX_SKILLS) break;
    // Reject the directory name too: it is what the SDK resolves plugin-
    // qualified skills by, so `..` must never get as far as a read.
    if (!isValidSkillName(entry)) continue;
    const skillFile = join(root, entry, "SKILL.md");
    try {
      if (!statSync(join(root, entry)).isDirectory()) continue;
      if (!existsSync(skillFile)) continue;
      if (statSync(skillFile).size > MAX_SKILL_FILE_BYTES) continue;
      const parsed = parseSkillFrontmatter(readFileSync(skillFile, "utf8"));
      // The frontmatter name must also match its directory: a bundle whose
      // doc claims a different name than the folder it lives in is ambiguous
      // to resolve and is not worth guessing about.
      if (parsed && parsed.name === entry) found.push(parsed);
    } catch {
      // An unreadable skill is skipped, never fatal: one bad file must not
      // take down every other skill in the bundle.
      continue;
    }
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
