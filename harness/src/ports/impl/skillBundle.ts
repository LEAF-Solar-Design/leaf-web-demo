/** Digest-verified curated skill bundle attachment for the converse lanes. */
import { createHash } from "node:crypto";
import { lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve, sep } from "node:path";

export type SkillTier = "tenant-safe" | "operator";

export type SkillBundleAttachment = {
  plugin: { type: "local"; path: string; skipMcpDiscovery: true };
  skills: string[];
  tier: SkillTier;
};

export type BundledSkill = { name: string; description: string };
export type VerifiedSkillBundle = {
  ok: true;
  tier: SkillTier;
  skills: BundledSkill[];
  digest: string;
  /**
   * The EXACT bytes that were hashed, keyed by skill name. The mount is written
   * from these and never re-reads the source. Reading the file again to copy it
   * would reopen the window verification just closed: hash, then copy, and a
   * concurrent writer slips between the two.
   */
  sources: Map<string, string>;
};
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

/**
 * Read one frontmatter value starting at `index`, returning it and the line to
 * resume from.
 *
 * Block scalars are the reason this is not a one-liner. Real curated skills
 * write `description: >-` and continue on indented lines, so reading only the
 * text after the colon yields the literal string ">-" and throws the actual
 * description away. That value is what the model reads to decide whether a
 * skill is relevant, so losing it disables the skill quietly rather than
 * loudly. Folded (`>`) joins its lines with spaces, literal (`|`) keeps the
 * newlines, and a blank line is a paragraph break in both.
 */
function readScalar(lines: string[], index: number, raw: string): { value: string; next: number } | null {
  const header = raw.trim();
  if (!/^[>|]/.test(header)) {
    return { value: header.replace(/^['"]|['"]$/g, ""), next: index + 1 };
  }
  // ONLY the four forms we can reproduce exactly. `>` and `>-` differ solely in
  // a trailing newline, which cannot change what a description says, so both
  // normalise to the same trimmed prose. Everything else is REFUSED rather than
  // approximated: `|+` exists precisely to keep trailing blank lines, and an
  // explicit indentation indicator (`>2`, `|-4`) means the leading spaces are
  // content. Guessing at either silently rewrites the text, and this module's
  // whole posture is that a shape we cannot reproduce is a bundle we do not
  // mount.
  if (!/^[>|]-?$/.test(header)) return null;
  const folded = header.startsWith(">");
  const content: string[] = [];
  let cursor = index + 1;
  for (; cursor < lines.length; cursor += 1) {
    const line = lines[cursor]!;
    // A line that is neither blank nor indented ends the block — that is where
    // YAML resumes reading keys, so the caller must resume there too or an
    // executable key hiding after a block scalar would never be inspected.
    if (line.trim() !== "" && !/^[ \t]/.test(line)) break;
    content.push(line);
  }
  const indentOf = (line: string): number => line.length - line.trimStart().length;
  const body = content.filter((line) => line.trim() !== "");
  if (body.length) {
    // A line indented FURTHER than the block's own indentation is structure
    // YAML keeps and folding would destroy. Refuse instead of flattening it.
    const base = indentOf(body[0]!);
    if (body.some((line) => indentOf(line) !== base)) return null;
  }
  let value = "";
  for (const line of content) {
    const text = line.trim();
    if (text === "") value += "\n";
    else value += (value === "" || value.endsWith("\n") ? "" : folded ? " " : "\n") + text;
  }
  return { value: value.trim(), next: cursor };
}

export function parseSkillFrontmatter(source: string): BundledSkill | null {
  const match = FRONTMATTER.exec(source);
  if (!match) return null;
  const lines = match[1].split(/\r?\n/);
  let name: string | null = null;
  let description: string | null = null;
  for (let index = 0; index < lines.length; index += 1) {
    const key = plainTopLevelKey(lines[index]!);
    if (!key) continue;
    if (EXECUTABLE_KEYS.has(key.toLowerCase())) return null;
    const line = lines[index]!;
    const scalar = readScalar(lines, index, line.slice(line.indexOf(":") + 1));
    if (!scalar) return null;
    index = scalar.next - 1;
    if (key === "name" && name === null) name = scalar.value;
    if (key === "description" && description === null) description = scalar.value;
  }
  return isValidSkillName(name) ? { name, description: description ?? "" } : null;
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
    // ONE read per file, kept. Everything downstream — the plugin manifest, the
    // frontmatter parse, and the bytes that get mounted — uses the buffer that
    // was hashed here. Re-reading any of them would mean the thing we checked
    // and the thing we use are two different reads of a file someone else can
    // write between them.
    const contents = new Map<string, Buffer>();
    for (const path of inventory) {
      const maxBytes = path === ".claude-plugin/plugin.json" ? MANIFEST_BYTES : SKILL_BYTES;
      if (lstatSync(files.get(path)!).size > maxBytes) return reject(`file exceeds size limit: ${path}`);
      const buffer = readFileSync(files.get(path)!);
      if (buffer.length > maxBytes) return reject(`file exceeds size limit: ${path}`);
      if (sha256(buffer) !== expectedFiles[path]) return reject(`hash mismatch: ${path}`);
      contents.set(path, buffer);
    }
    if (typeof manifest.bundleDigest !== "string" || !SHA256.test(manifest.bundleDigest) || manifest.bundleDigest !== digest(expectedFiles as Record<string, string>)) return reject("bundle digest mismatch");
    if (options.expectedDigest !== undefined && options.expectedDigest !== manifest.bundleDigest) return reject("bundle digest does not match deployment pin");

    const pluginBuffer = contents.get(".claude-plugin/plugin.json");
    if (!pluginBuffer) return reject("plugin.json is missing from the verified inventory");
    let plugin: Record<string, unknown>;
    try { plugin = JSON.parse(pluginBuffer.toString("utf8")) as Record<string, unknown>; } catch { return reject("plugin.json is invalid JSON"); }
    if (!plugin || Array.isArray(plugin) || Object.keys(plugin).some((key) => !PLUGIN_KEYS.has(key))) return reject("plugin.json has disallowed keys");
    if (typeof plugin.name !== "string" || typeof plugin.version !== "string" || (plugin.description !== undefined && typeof plugin.description !== "string") || plugin.leafTier !== manifest.tier) return reject("plugin.json is invalid or tier-mismatched");

    const skills: BundledSkill[] = [];
    const sources = new Map<string, string>();
    for (const name of skillNames) {
      const buffer = contents.get(`skills/${name}/SKILL.md`);
      if (!buffer) return reject(`skill is missing from the verified inventory: ${name}`);
      const text = buffer.toString("utf8");
      const parsed = parseSkillFrontmatter(text);
      if (!parsed || parsed.name !== name) return reject(`invalid skill frontmatter: ${name}`);
      skills.push(parsed);
      sources.set(name, text);
    }
    return { ok: true, tier: manifest.tier, skills, digest: manifest.bundleDigest, sources };
  } catch {
    return reject("bundle cannot be safely read");
  }
}

export function discoverSkills(bundlePath: string): BundledSkill[] {
  const verified = verifyBundle(bundlePath);
  return verified.ok ? verified.skills : [];
}

/**
 * Materialise a PRIVATE, NORMALISED copy of a verified bundle and return its
 * path. This is what actually gets mounted, and it closes two findings at once:
 *
 * 1. TOCTOU. Verifying a directory and then handing the SDK that same mutable
 *    directory proves nothing: SKILL.md or plugin.json can be swapped between
 *    the hash check and the SDK's read, and a deployment digest pin does not
 *    help. The SDK now loads bytes WE wrote, after verification, into a
 *    directory the tenant does not control.
 *
 * 2. Frontmatter. Inspecting YAML for forbidden keys kept losing — a quoted
 *    key, a unicode escape, an explicit `? key` mapping all resolve to the same
 *    top-level key the SDK sees while evading a textual check, and pulling in a
 *    real YAML parser would add a dependency (and CVE surface) to a module
 *    whose whole virtue is that it only touches node:fs. So the snapshot is
 *    REWRITTEN rather than inspected: each SKILL.md is emitted with frontmatter
 *    containing exactly `name` and `description`, values re-serialised as JSON
 *    strings, followed by the original body. Whatever the source declared —
 *    hooks, context: fork, monitors — cannot survive a document we construct.
 *    Allowlist by construction, which is the only version of this that has not
 *    been picked apart.
 */
export function materialiseVerifiedBundle(verified: VerifiedSkillBundle): string | null {
  let root: string | null = null;
  try {
    root = mkdtempSync(join(tmpdir(), "leaf-skills-mount-"));
    mkdirSync(join(root, ".claude-plugin"), { recursive: true });
    // A manifest we author, carrying only the keys the loader allowlists.
    writeFileSync(
      join(root, ".claude-plugin", "plugin.json"),
      JSON.stringify({ name: "leaf-skills", version: "1.0.0", leafTier: verified.tier }, null, 2),
      { mode: 0o400 },
    );
    for (const skill of verified.skills) {
      const dir = join(root, "skills", skill.name);
      mkdirSync(dir, { recursive: true });
      // The verified buffer, NOT a fresh read of the source file.
      const body = verified.sources.get(skill.name);
      if (body === undefined) throw new Error(`no verified bytes for ${skill.name}`);
      const rewritten =
        "---\n" +
        `name: ${JSON.stringify(skill.name)}\n` +
        `description: ${JSON.stringify(skill.description)}\n` +
        "---\n" +
        stripFrontmatter(body);
      writeFileSync(join(dir, "SKILL.md"), rewritten, { mode: 0o400 });
    }
    // THREAT MODEL, stated plainly: mode 0400 and a 0700 mkdtemp root keep other
    // OS users out, and that is the whole claim. They do not stop the harness's
    // OWN account from rewriting the snapshot — but an adversary already running
    // as the harness does not need a skill to do anything. What this closes is
    // the tenant-writable bundle directory, which is the untrusted input.
    mounted.add(root);
    armMountCleanup();
    return root;
  } catch {
    if (root) { try { rmSync(root, { recursive: true, force: true }); } catch { /* best effort */ } }
    return null;
  }
}

/** Snapshots this process created, removed together when it exits. */
const mounted = new Set<string>();
let cleanupArmed = false;

function armMountCleanup(): void {
  if (cleanupArmed) return;
  cleanupArmed = true;
  process.on("exit", () => {
    for (const dir of mounted) {
      try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
    }
  });
}

/** Everything after the frontmatter block — the prose the skill is FOR. */
function stripFrontmatter(source: string): string {
  const match = FRONTMATTER.exec(source ?? "");
  return match ? source.slice(match[0].length).replace(/^\r?\n/, "") : source;
}

/**
 * One snapshot per configuration, not one per turn.
 *
 * Both runners call this on EVERY turn. Without the memo each turn re-walked
 * and re-hashed the whole bundle and left another temp copy behind: an
 * unbounded disk and inode leak on a long-lived server, paid for with a full
 * bundle hash on the hot path. The result is a pure function of
 * (path, tier, pin) — the pin names one exact artifact — so it is computed
 * once. Refusals are memoised too, which also keeps the refusal from
 * reprinting on every turn.
 */
const mounts = new Map<string, SkillBundleAttachment>();
/** Bundle paths already reported as unpinned, so the refusal is logged once. */
const warned = new Set<string>();

export function skillBundleAttachment(env: NodeJS.ProcessEnv = process.env): SkillBundleAttachment | null {
  const path = env.LEAF_SKILLS_BUNDLE_PATH?.trim();
  const tier = env.LEAF_SKILLS_TIER?.trim();
  const pin = env.LEAF_SKILLS_BUNDLE_DIGEST?.trim();
  const key = JSON.stringify([path ?? "", tier ?? "", pin ?? ""]);
  const cached = mounts.get(key);
  if (cached) return cached;
  const attachment = mountFor(path, tier, pin);
  // Only a SUCCESS is remembered. A cached refusal would mean an operator who
  // repairs the bundle in place has to restart the process before it is ever
  // picked up, and the refusal path is cheap in the case that actually repeats
  // (no bundle configured returns before touching the disk). Keyed by
  // configuration rather than held in one slot, so alternating configurations
  // reuse their snapshots instead of making new ones.
  if (attachment) mounts.set(key, attachment);
  return attachment;
}

function mountFor(
  path: string | undefined,
  tier: string | undefined,
  pin: string | undefined,
): SkillBundleAttachment | null {
  if (!path || (tier !== "tenant-safe" && tier !== "operator")) return null;
  // The PIN IS REQUIRED. A self-consistent bundle proves nothing about
  // provenance: anyone who can write the directory can also regenerate the
  // manifest and its digest. Only a digest the DEPLOYMENT states out of band
  // says "this exact artifact was approved". No pin, no mount.
  if (!pin) {
    // Once per configuration, not once per turn: this is now re-evaluated on
    // every turn so that a fix is picked up, and a log line on every turn would
    // bury the one that matters.
    if (!warned.has(path)) {
      warned.add(path);
      console.error(
      "[leaf-skills] refusing to mount: LEAF_SKILLS_BUNDLE_DIGEST is required — " +
        "a self-verifying bundle proves consistency, not provenance",
      );
    }
    return null;
  }
  const verified = verifyBundle(path, { expectedDigest: pin });
  if (!verified.ok || verified.tier !== tier || verified.skills.length === 0) return null;
  const mountPath = materialiseVerifiedBundle(verified);
  if (!mountPath) return null;
  return {
    plugin: { type: "local", path: mountPath, skipMcpDiscovery: true },
    skills: verified.skills.map((skill) => skill.name),
    tier: verified.tier,
  };
}
