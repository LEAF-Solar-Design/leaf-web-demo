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
/**
 * YAML scalars that are NOT strings. A plain `false`, `null`, `3.14` or `[a, b]`
 * is a boolean, a null, a number or a sequence, so reproducing it as the text
 * between the colon and the newline would put something in the mounted document
 * that the source did not say.
 */
const NON_STRING_PLAIN =
  /^(null|~|true|false|yes|no|on|off|[-+]?\d+(\.\d*)?([eE][-+]?\d+)?|0x[0-9a-fA-F]+|0o[0-7]+|-?\.inf|\.nan|\[.*\]|\{.*\})$/i;

/**
 * Decode a one-line YAML scalar, or return null meaning "present, but we will
 * not guess at it".
 *
 * Stripping the outer quotes is not decoding: `"a\nb"` is a newline to YAML and
 * a backslash-n to a naive strip, `'it''s'` is one apostrophe, and in a plain
 * scalar ` #` begins a comment. YAML separates on SPACE AND TAB only, so
 * JavaScript's `\s` — which includes NBSP and U+2028 — would cut a value where
 * YAML does not.
 */
function readInlineScalar(raw: string): string | null {
  const value = raw.replace(/^[ \t]+|[ \t]+$/g, "");
  if (value.startsWith('"')) {
    if (value.length < 2 || !/(^|[^\\])(\\\\)*"$/.test(value)) return null;
    let out = "";
    for (let i = 1; i < value.length - 1; i += 1) {
      const ch = value[i]!;
      // An unescaped quote before the end means this is not one scalar at all.
      if (ch === '"') return null;
      if (ch !== "\\") { out += ch; continue; }
      const esc = value[++i];
      if (esc === "u") {
        const hex = value.slice(i + 1, i + 5);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) return null;
        out += String.fromCharCode(parseInt(hex, 16));
        i += 4;
        continue;
      }
      const simple: Record<string, string> = {
        n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "/": "/",
        b: "\b", f: "\f", "0": "\0", " ": " ",
      };
      const mapped = esc === undefined ? undefined : simple[esc];
      // An escape we do not decode is one we would reproduce wrongly.
      if (mapped === undefined) return null;
      out += mapped;
    }
    return out;
  }
  if (value.startsWith("'")) {
    if (value.length < 2 || !value.endsWith("'")) return null;
    const inner = value.slice(1, -1);
    // A doubled quote is the only escape here, so a lone one means the value is
    // not what it looks like.
    if (inner.replace(/''/g, "").includes("'")) return null;
    return inner.replace(/''/g, "'");
  }
  const comment = value.search(/(^|[ \t])#/);
  const text = (comment === -1 ? value : value.slice(0, comment)).replace(/[ \t]+$/, "");
  if (text === "" || /^[&*!%@`]/.test(text) || NON_STRING_PLAIN.test(text)) return null;
  return text;
}

function readScalar(lines: string[], index: number, raw: string): { value: string | null; next: number } {
  const header = raw.replace(/^[ \t]+|[ \t]+$/g, "");
  if (!/^[>|]/.test(header)) {
    return { value: readInlineScalar(header), next: index + 1 };
  }
  // ONLY the four plain headers, and then only a block whose text we can
  // reproduce CHARACTER FOR CHARACTER. Everything else refuses. An explicit
  // indentation indicator (`>2`, `|-4`) makes leading spaces content; `+` keeps
  // trailing blank lines on purpose; a more-indented line inside a folded block
  // is preserved verbatim by YAML rather than folded; and a tab is not YAML
  // indentation at all. Each of those needs a real YAML engine to honour, and
  // approximating any of them silently rewrites the description the model reads
  // to decide a skill applies. Refusing is the honest option and it is loud:
  // the bundle does not build.
  const reproducible = /^[>|]-?$/.test(header);
  const folded = header.startsWith(">");
  const chomped = header.endsWith("-");
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
  // Blank lines AFTER the text are chomped by YAML (`-` drops them, clip keeps
  // exactly one newline), so they carry no content and a blank line before the
  // next key is ordinary formatting. Blank lines BETWEEN text lines are content.
  while (content.length && content[content.length - 1]!.trim() === "") content.pop();
  // An unreproducible block still has a knowable EXTENT, and the caller has to
  // resume after it either way or a key hiding below would never be inspected.
  if (!reproducible) return { value: null, next: cursor };
  if (!content.length) return { value: "", next: cursor };
  const base = content[0]!.length - content[0]!.trimStart().length;
  const parts: string[] = [];
  for (const line of content) {
    if (line.trim() === "") return { value: null, next: cursor };  // interior blank
    if (/\t/.test(line.slice(0, base + 1))) return { value: null, next: cursor };  // tab indent
    if (line.length - line.trimStart().length !== base) return { value: null, next: cursor };
    if (/\s$/.test(line)) return { value: null, next: cursor };  // trailing space
    parts.push(line.slice(base));
  }
  // Clip (`>` / `|`) keeps exactly one trailing newline; strip (`-`) keeps none.
  // Emitting it makes the reproduction exact rather than nearly exact.
  return { value: parts.join(folded ? " " : "\n") + (chomped ? "" : "\n"), next: cursor };
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
    index = scalar.next - 1;
    // Only `name` and `description` are re-emitted into the mounted document,
    // so only those two must be reproducible. Refusing a skill because some
    // unrelated key holds `false` would reject a third of the real corpus for
    // nothing; refusing one we would have to GUESS at is the whole point.
    if (key === "name" && name === null) {
      if (scalar.value === null) return null;
      name = scalar.value;
    }
    if (key === "description" && description === null) {
      if (scalar.value === null) return null;
      description = scalar.value;
    }
  }
  // A description is REQUIRED, not decorative: it is the only text the model
  // reads when deciding whether a skill applies, so a skill without one is
  // mounted and never chosen. Silently useless is the failure mode this whole
  // module is built to avoid, and every curated skill already has one — an
  // empty description means the artifact is malformed, not minimal.
  if (!isValidSkillName(name) || !description) return null;
  return { name, description };
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
    // An empty bundle is not a valid artifact, and this belongs HERE rather
    // than at mount time: the offline verifier checks the same shape, so a
    // rule only the mount applies is a rule the build gate cannot mirror.
    if (skillNames.length === 0) return reject("bundle contains no skills");
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
const MAX_CACHED_MOUNTS = 4;
const mounts = new Map<string, SkillBundleAttachment>();
/**
 * Refusals already reported, keyed by path AND reason. Logged once per distinct
 * reason rather than once per turn: the check re-runs every turn so a repair is
 * picked up, and a line per turn would bury the one that matters — but a NEW
 * reason is new information and gets its own line.
 */
const warned = new Set<string>();

/** The most distinct refusals to report before going quiet. */
/**
 * Everything a log reader might act on rather than display: C0 and C1
 * controls (cursor moves, escape sequences), U+2028 and U+2029 which are
 * LINE SEPARATORS to a JavaScript or JSON reader and so can forge a log
 * entry, and the bidi overrides U+200E, U+200F, U+202A-202E and U+2066-2069,
 * which reorder visible text so a reader sees something other than what is
 * written.
 */
const CONTROL_CHARACTERS =
  /[\u0000-\u001f\u007f-\u009f\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]/g;

const MAX_WARNINGS = 32;

/**
 * A refusal reason quotes FILENAMES FROM A TENANT-WRITABLE DIRECTORY. Written
 * raw to a terminal or a log pipeline, a crafted name can move the cursor,
 * inject a fake line, or emit escape sequences the reader interprets — a log is
 * a place attacker-controlled text is read by people and by parsers. Control
 * characters are escaped and the reason is truncated. The count is capped too:
 * a hostile name that changes every turn would otherwise grow both the log and
 * this set without limit.
 */
/**
 * Make a refusal reason safe to write to a log.
 *
 * Reasons quote FILENAMES FROM A TENANT-WRITABLE DIRECTORY. Written raw to a
 * terminal or a log pipeline, a crafted name can move the cursor, forge a
 * line, or emit escape sequences the reader interprets — logs are read by
 * people and by parsers, and both can be lied to. Exported so the escaping
 * is tested directly: routing a control character through a real filename
 * only works on filesystems that accept one.
 */
export function sanitiseLogText(reason: string): string {
  // TRUNCATE FIRST. Slicing after expansion can cut an escape in half and
  // leave a trailing `\\x` for a parser to trip over; slicing the input means
  // every escape in the output is whole.
  return reason.slice(0, 300).replace(CONTROL_CHARACTERS, (ch) => {
    const code = ch.charCodeAt(0);
    return code > 0xff
      ? "\\u" + code.toString(16).padStart(4, "0")
      : "\\x" + code.toString(16).padStart(2, "0");
  });
}

function refuse(path: string, reason: string): null {
  const safe = sanitiseLogText(reason);
  const key = path + "\u0000" + safe;
  if (!warned.has(key) && warned.size < MAX_WARNINGS) {
    warned.add(key);
    console.error(`[leaf-skills] refusing to mount: ${safe}`);
  }
  return null;
}

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
  // (no bundle configured returns before touching the disk).
  if (attachment) {
    // BOUNDED. Production reads one process-wide configuration, so this holds a
    // single entry — but the function is exported and takes an env object, so an
    // explicit caller (or a long test run) could otherwise grow it, and every
    // key holds a snapshot directory until exit. Evicting oldest-first removes
    // the directory with it.
    if (mounts.size >= MAX_CACHED_MOUNTS) {
      const oldest = mounts.keys().next().value as string | undefined;
      if (oldest !== undefined) mounts.delete(oldest);
      // The entry goes; the DIRECTORY STAYS until process exit. An earlier
      // sdk.query may still be reading that path, and removing it underneath a
      // live turn trades a bounded map for a broken session. Production holds
      // one configuration and never evicts, so this only ever costs a stale
      // directory that exit cleanup already owns.
    }
    mounts.set(key, attachment);
  }
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
    return refuse(path,
      `${path}: LEAF_SKILLS_BUNDLE_DIGEST is required — a self-verifying ` +
      "bundle proves consistency, not provenance");
  }
  const verified = verifyBundle(path, { expectedDigest: pin });
  // SAY WHY. A refusal removes every skill, and no-skills looks exactly like a
  // deployment that never configured any — so a tampered byte, or a description
  // in a YAML form we will not reproduce, would disable the feature with
  // nothing in the log to find. verifyBundle already returns a reason; the only
  // failure worth having is the one an operator can read.
  if (!verified.ok) return refuse(path, `${path}: ${verified.reason}`);
  if (verified.tier !== tier) {
    return refuse(path, `${path}: bundle tier ${verified.tier} does not match LEAF_SKILLS_TIER ${tier}`);
  }
  const mountPath = materialiseVerifiedBundle(verified);
  if (!mountPath) return refuse(path, `${path}: the verified snapshot could not be written`);
  return {
    plugin: { type: "local", path: mountPath, skipMcpDiscovery: true },
    skills: verified.skills.map((skill) => skill.name),
    tier: verified.tier,
  };
}
