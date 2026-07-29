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
 * Three things the spike settled, each of which this module encodes:
 *
 * 1. `settingSources` CANNOT point at a curated directory — its type is
 *    'user' | 'project' | 'local', and 'user' IS ~/.claude. It stays `[]`;
 *    `plugins` is what supplies skills from an arbitrary path. (The build plan
 *    originally assumed a "scoped settingSources"; that was wrong.)
 *
 * 2. `skills: 'all'` also enables Claude Code's BUNDLED skills (run, review,
 *    security-review, …) — developer tooling that has no business in a tenant
 *    session. An explicit allowlist was verified EXCLUSIVE, so this module
 *    always emits a list, never 'all'.
 *
 * 3. `skills` is a CONTEXT FILTER, NOT A SANDBOX: per the SDK, unlisted skills
 *    are hidden from the listing and refused by the Skill tool, but their FILES
 *    REMAIN ON DISK and are reachable via Read/Bash. So the tenant/operator
 *    split is enforced by WHICH BUNDLE IS MOUNTED, never by filtering one
 *    superset — hence one bundle path per tier, resolved outside this module.
 *
 * OFF unless configured. No bundle path, no attachment, and the session runs
 * exactly as it does today.
 */
import { readdirSync, readFileSync, existsSync, statSync } from "node:fs";
import { join } from "node:path";

export type SkillBundleAttachment = {
  /** SDK `plugins` entry supplying the bundle's skills. */
  plugin: { type: "local"; path: string; skipMcpDiscovery: true };
  /** SDK `skills` allowlist — always explicit, never 'all'. */
  skills: string[];
};

/** A skill discovered in a bundle, for the registry/UI to list. */
export type BundledSkill = { name: string; description: string };

const SKILLS_DIR = "skills";
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---/;

/** Pull `name` / `description` out of a SKILL.md's YAML frontmatter.
 *
 * Deliberately a line scan, not a YAML parser: the two fields are flat scalars
 * and adding a YAML dependency to the harness for a two-key read would be a
 * new transitive tree (and CVE surface) for nothing.
 */
export function parseSkillFrontmatter(source: string): BundledSkill | null {
  const match = FRONTMATTER.exec(source ?? "");
  if (!match) return null;
  let name = "";
  let description = "";
  for (const line of match[1].split(/\r?\n/)) {
    const kv = /^(name|description)\s*:\s*(.*)$/.exec(line.trim());
    if (!kv) continue;
    const value = kv[2].trim().replace(/^["']|["']$/g, "");
    if (kv[1] === "name") name = value;
    else description = value;
  }
  if (!name) return null;
  return { name, description };
}

/**
 * Discover the skills a bundle actually contains.
 *
 * The allowlist is built from what is ON DISK rather than a hand-maintained
 * list: a name in the list that is not in the bundle would be silently
 * unavailable, and a skill in the bundle missing from the list would be
 * invisible. Reading the directory keeps those in step by construction.
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
    const skillFile = join(root, entry, "SKILL.md");
    try {
      if (!statSync(join(root, entry)).isDirectory()) continue;
      if (!existsSync(skillFile)) continue;
      const parsed = parseSkillFrontmatter(readFileSync(skillFile, "utf8"));
      if (parsed) found.push(parsed);
    } catch {
      // An unreadable skill is skipped, never fatal: one bad file must not
      // take down every other skill in the bundle.
      continue;
    }
  }
  return found.sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * Build the SDK attachment for a tier's bundle, or null when unconfigured.
 *
 * `LEAF_SKILLS_BUNDLE_PATH` is resolved per TIER by the caller — the tenant
 * bundle and the operator bundle are different directories, because the split
 * is a disk boundary (see note 3 above), not a filter.
 */
export function skillBundleAttachment(
  env: NodeJS.ProcessEnv = process.env,
): SkillBundleAttachment | null {
  const path = env.LEAF_SKILLS_BUNDLE_PATH?.trim();
  if (!path) return null;
  if (!existsSync(path)) return null;
  const skills = discoverSkills(path).map((s) => s.name);
  // An empty bundle attaches nothing: enabling the Skill tool with no skills
  // behind it would add a capability the model can only fail with.
  if (skills.length === 0) return null;
  return {
    plugin: { type: "local", path, skipMcpDiscovery: true },
    skills,
  };
}
