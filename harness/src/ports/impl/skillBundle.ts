/** Digest-verified curated skill bundle attachment for the converse lanes. */
import { createHash } from "node:crypto";
import { lstatSync, readdirSync, readFileSync } from "node:fs";
import { join, relative, resolve, sep } from "node:path";

export type SkillTier = "tenant-safe" | "operator";

export type SkillBundleAttachment = {
  plugin: { type: "local"; path: string; skipMcpDiscovery: true };
  skills: string[];
  tier: SkillTier;
};

export type BundledSkill = { name: string; description: string };
export type VerifiedSkillBundle = { ok: true; tier: SkillTier; skills: BundledSkill[]; digest: string };
export type RejectedSkillBundle = { ok: false; reason: string };
export type VerifyBundleResult = VerifiedSkillBundle | RejectedSkillBundle;

const MANIFEST_BYTES = 64 * 1024;
const SKILL_BYTES = 256 * 1024;
const MAX_SKILLS = 250;
const SHA256 = /^[a-f0-9]{64}$/;
const FRONTMATTER = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/;
const EXECUTABLE_KEYS = new Set([
  "hooks", "allowed-tools", "allowedtools", "context", "agent", "agents",
  "background", "monitor", "monitors", "command", "commands", "mcp", "mcpservers",
]);
const PLUGIN_KEYS = new Set(["name", "version", "description", "leafTier"]);
const RESERVED_NAMES = new Set([
  "con", "prn", "aux", "nul",
  ...Array.from({ length: 9 }, (_, i) => `com${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `lpt${i + 1}`),
]);

export const SKILL_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/i;

export function isValidSkillName(name: unknown): name is string {
  if (typeof name !== "string" || !SKILL_NAME_RE.test(name) || name.endsWith(".")) return false;
  return !RESERVED_NAMES.has(name.split(".")[0].toLowerCase());
}

function reject(reason: string): RejectedSkillBundle {
  return { ok: false, reason };
}

function sha256(data: Buffer): string {
  return createHash("sha256").update(data).digest("hex");
}

function digest(files: Record<string, string>): string {
  return sha256(Buffer.from(Object.keys(files).sort().map((path) => `${path}:${files[path]}`).join("\n")));
}

function plainTopLevelKey(line: string): string | null {
  if (/^\s/.test(line) || line.trim() === "" || line.trimStart().startsWith("#")) return null;
  const match = /^(?:"([^"]+)"|'([^']+)'|([^:\s][^:]*?))\s*:/.exec(line);
  return (match?.[1] ?? match?.[2] ?? match?.[3] ?? null)?.trim() ?? null;
}

export function parseSkillFrontmatter(source: string): BundledSkill | null {
  const match = FRONTMATTER.exec(source);
  if (!match) return null;
  let name: string | null = null;
  let description = "";
  for (const line of match[1].split(/\r?\n/)) {
    const key = plainTopLevelKey(line);
    if (!key) continue;
    if (EXECUTABLE_KEYS.has(key.toLowerCase())) return null;
    const value = line.slice(line.indexOf(":") + 1).trim().replace(/^['"]|['"]$/g, "");
    if (key === "name" && name === null) name = value;
    if (key === "description" && description === "") description = value;
  }
  return isValidSkillName(name) ? { name, description } : null;
}

/**
 * Verify the exact artifact made by tools/skills-bundle/build.mjs.
 *
 * manifest.json intentionally is not included in its own file inventory. The
 * builder writes it last, so hashing it would require an impossible recursive
 * self-reference. Every other on-disk file is listed and hashed.
 */
export function verifyBundle(bundlePath: string, options: { expectedDigest?: string } = {}): VerifyBundleResult {
  const root = resolve(bundlePath);
  const files = new Map<string, string>();
  try {
    const rootStat = lstatSync(root);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) return reject("bundle root is not a plain directory");

    const walk = (dir: string): string | null => {
      for (const entry of readdirSync(dir)) {
        const path = join(dir, entry);
        const stat = lstatSync(path);
        const rel = relative(root, path).split(sep).join("/");
        if (stat.isSymbolicLink() || stat.isSocket() || stat.isFIFO() || stat.isBlockDevice() || stat.isCharacterDevice()) {
          return `unsafe filesystem entry: ${rel}`;
        }
        if (stat.isDirectory()) {
          const problem = walk(path);
          if (problem) return problem;
          continue;
        }
        if (!stat.isFile()) return `non-regular filesystem entry: ${rel}`;
        if (stat.nlink > 1) return `hardlinked file: ${rel}`;
        files.set(rel, path);
      }
      return null;
    };
    const unsafe = walk(root);
    if (unsafe) return reject(unsafe);

    const rootEntries = readdirSync(root).sort();
    if (rootEntries.join("\0") !== [".claude-plugin", "manifest.json", "skills"].join("\0")) return reject("invalid bundle root shape");
    const pluginDir = join(root, ".claude-plugin");
    if (!lstatSync(pluginDir).isDirectory() || readdirSync(pluginDir).join("\0") !== "plugin.json") return reject("invalid plugin directory shape");
    const skillsDir = join(root, "skills");
    if (!lstatSync(skillsDir).isDirectory()) return reject("skills is not a directory");
    const skillNames = readdirSync(skillsDir).sort();
    if (skillNames.length > MAX_SKILLS) return reject("bundle exceeds skill limit");
    const seen = new Set<string>();
    for (const name of skillNames) {
      if (!isValidSkillName(name) || seen.has(name.toLowerCase())) return reject(`invalid skill name: ${name}`);
      seen.add(name.toLowerCase());
      const dir = join(skillsDir, name);
      if (!lstatSync(dir).isDirectory() || readdirSync(dir).join("\0") !== "SKILL.md") return reject(`invalid skill shape: ${name}`);
    }

    const manifestPath = join(root, "manifest.json");
    if (lstatSync(manifestPath).size > MANIFEST_BYTES) return reject("manifest.json exceeds size limit");
    const manifestBuffer = readFileSync(manifestPath);
    let manifest: { version?: unknown; tier?: unknown; files?: unknown; bundleDigest?: unknown };
    try { manifest = JSON.parse(manifestBuffer.toString("utf8")); } catch { return reject("manifest.json is invalid JSON"); }
    if (!manifest || manifest.version !== 1 || (manifest.tier !== "tenant-safe" && manifest.tier !== "operator")) return reject("manifest has invalid version or tier");
    if (!manifest.files || typeof manifest.files !== "object" || Array.isArray(manifest.files)) return reject("manifest files is not an object");
    const expectedFiles = manifest.files as Record<string, unknown>;
    if (Object.values(expectedFiles).some((hash) => typeof hash !== "string" || !SHA256.test(hash))) return reject("manifest has invalid file hash");

    const inventory = [...files.keys()].filter((path) => path !== "manifest.json").sort();
    const declared = Object.keys(expectedFiles).sort();
    if (declared.length !== inventory.length || declared.some((path, index) => path !== inventory[index])) return reject("manifest file list does not exactly match disk");
    for (const path of inventory) {
      const maxBytes = path === ".claude-plugin/plugin.json" ? MANIFEST_BYTES : SKILL_BYTES;
      if (lstatSync(files.get(path)!).size > maxBytes) return reject(`file exceeds size limit: ${path}`);
      if (sha256(readFileSync(files.get(path)!)) !== expectedFiles[path]) return reject(`hash mismatch: ${path}`);
    }
    if (typeof manifest.bundleDigest !== "string" || !SHA256.test(manifest.bundleDigest) || manifest.bundleDigest !== digest(expectedFiles as Record<string, string>)) return reject("bundle digest mismatch");
    if (options.expectedDigest !== undefined && options.expectedDigest !== manifest.bundleDigest) return reject("bundle digest does not match deployment pin");

    const pluginBuffer = readFileSync(join(pluginDir, "plugin.json"));
    let plugin: Record<string, unknown>;
    try { plugin = JSON.parse(pluginBuffer.toString("utf8")) as Record<string, unknown>; } catch { return reject("plugin.json is invalid JSON"); }
    if (!plugin || Array.isArray(plugin) || Object.keys(plugin).some((key) => !PLUGIN_KEYS.has(key))) return reject("plugin.json has disallowed keys");
    if (typeof plugin.name !== "string" || typeof plugin.version !== "string" || (plugin.description !== undefined && typeof plugin.description !== "string") || plugin.leafTier !== manifest.tier) return reject("plugin.json is invalid or tier-mismatched");

    const skills: BundledSkill[] = [];
    for (const name of skillNames) {
      const source = readFileSync(join(skillsDir, name, "SKILL.md"));
      const parsed = parseSkillFrontmatter(source.toString("utf8"));
      if (!parsed || parsed.name !== name) return reject(`invalid skill frontmatter: ${name}`);
      skills.push(parsed);
    }
    return { ok: true, tier: manifest.tier, skills, digest: manifest.bundleDigest };
  } catch {
    return reject("bundle cannot be safely read");
  }
}

export function discoverSkills(bundlePath: string): BundledSkill[] {
  const verified = verifyBundle(bundlePath);
  return verified.ok ? verified.skills : [];
}

export function skillBundleAttachment(env: NodeJS.ProcessEnv = process.env): SkillBundleAttachment | null {
  const path = env.LEAF_SKILLS_BUNDLE_PATH?.trim();
  const tier = env.LEAF_SKILLS_TIER?.trim();
  if (!path || (tier !== "tenant-safe" && tier !== "operator")) return null;
  const verified = verifyBundle(path, { expectedDigest: env.LEAF_SKILLS_BUNDLE_DIGEST?.trim() || undefined });
  if (!verified.ok || verified.tier !== tier || verified.skills.length === 0) return null;
  return { plugin: { type: "local", path, skipMcpDiscovery: true }, skills: verified.skills.map((skill) => skill.name), tier: verified.tier };
}
