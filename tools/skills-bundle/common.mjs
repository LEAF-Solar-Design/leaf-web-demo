import { createHash } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";

export const MAX_SKILL_FILE_BYTES = 256 * 1024;
// Mirrors harness/src/ports/impl/skillBundle.ts, the loader source of truth.
export const MAX_SKILLS = 250;
export const MAX_MANIFEST_BYTES = 64 * 1024;
export const SKILL_NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/i;
const RESERVED_NAMES = new Set([
  "con", "prn", "aux", "nul",
  ...Array.from({ length: 9 }, (_, index) => `com${index + 1}`),
  ...Array.from({ length: 9 }, (_, index) => `lpt${index + 1}`),
]);

// Both of these mirror harness/src/ports/impl/skillBundle.ts. Two
// implementations of one rule set drift silently, so skillBundle.test.ts
// cross-checks that the loader and this verifier accept and reject the same
// hostile corpus. Change one, change both, and widen that corpus.
export const PLUGIN_KEYS = new Set(["name", "version", "description", "leafTier"]);
// Frontmatter keys that make a skill EXECUTABLE or spawn something rather than
// instruct. A curated skill is prose. The runtime flags do not cover all of
// these: `context: fork` starts a SUBAGENT and neither
// disableSkillShellExecution nor disableAllHooks touches it.
export const EXECUTABLE_FRONTMATTER_KEYS = new Set([
  "hooks", "allowed-tools", "allowedtools", "context", "agent", "agents",
  "background", "monitor", "monitors", "command", "commands", "mcp", "mcpservers",
]);

export function fail(message) {
  throw new Error(message);
}

/**
 * The top-level YAML key a line declares, or null for a blank line, a comment,
 * or an indented (nested) line. Quotes are stripped: `"context": fork` and
 * `context: fork` name the same key, and a check that reads only bare keys
 * lets the quoted form straight through.
 */
export function plainTopLevelKey(line) {
  if (/^\s/.test(line) || line.trim() === "" || line.trimStart().startsWith("#")) return null;
  const match = /^(?:"([^"]+)"|'([^']+)'|([^:\s][^:]*?))\s*:/.exec(line);
  return (match?.[1] ?? match?.[2] ?? match?.[3] ?? null)?.trim() ?? null;
}

export function isValidSkillName(name) {
  if (typeof name !== "string" || !SKILL_NAME_RE.test(name) || name.endsWith(".")) {
    return false;
  }
  return !RESERVED_NAMES.has(name.split(".")[0].toLowerCase());
}

export function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

export function bundleDigest(files) {
  return sha256(Object.keys(files).sort().map((name) => `${name}:${files[name]}`).join("\n"));
}

export async function assertPlainFile(filePath, label, maxBytes = Infinity) {
  let stat;
  try {
    stat = await fs.lstat(filePath);
  } catch {
    fail(`${label} is missing: ${filePath}`);
  }
  if (stat.isSymbolicLink()) fail(`${label} must not be a symlink: ${filePath}`);
  if (!stat.isFile()) fail(`${label} must be a regular file: ${filePath}`);
  if (stat.nlink > 1) fail(`${label} must not be hardlinked: ${filePath}`);
  if (stat.size > maxBytes) fail(`${label} exceeds ${maxBytes} bytes: ${filePath}`);
  return stat;
}

export async function assertPlainDirectory(dirPath, label) {
  let stat;
  try {
    stat = await fs.lstat(dirPath);
  } catch {
    fail(`${label} is missing: ${dirPath}`);
  }
  if (stat.isSymbolicLink()) fail(`${label} must not be a symlink: ${dirPath}`);
  if (!stat.isDirectory()) fail(`${label} must be a directory: ${dirPath}`);
}

export function parseSkillName(markdown, label) {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(markdown);
  if (!frontmatter) fail(`${label} lacks YAML frontmatter`);
  for (const line of frontmatter[1].split(/\r?\n/)) {
    const match = /^name\s*:\s*(.*)$/.exec(line.trim());
    if (!match) continue;
    return match[1].trim().replace(/^["']|["']$/g, "");
  }
  fail(`${label} lacks a frontmatter name`);
}

/**
 * Byte-for-byte the loader's list. Follows js-yaml's resolvers, because the
 * near-misses are the problem: `.5` and `+.inf` are floats, `0b1010` is an
 * int, `1_000` is one thousand, `1:30` is sexagesimal.
 */
const NON_STRING_PLAIN =
  /^(null|~|true|false|yes|no|on|off|[-+]?(0b[01_]+|0o?[0-7_]+|0x[0-9a-fA-F_]+|[0-9][0-9_]*(:[0-5]?[0-9])+)|[-+]?([0-9][0-9_]*(\.[0-9_]*)?|\.[0-9_]+)([eE][-+]?[0-9]+)?|[-+]?\.inf|\.nan|\[.*\]|\{.*\}|\d{4}-\d\d?-\d\d?(([Tt]|[ \t]+)\d\d?:\d\d:\d\d(\.\d+)?([ \t]*([Zz]|[-+]\d\d?(:?\d\d)?))?)?)$/i;

/**
 * Byte-for-byte the loader's `readInlineScalar`
 * (harness/src/ports/impl/skillBundle.ts). Stripping the outer quotes is not
 * decoding, YAML separates on space and tab only, and a plain `false` is a
 * boolean rather than the text "false". Any difference here is a bundle the
 * build gate calls READY and production refuses.
 */
export function readInlineScalar(raw) {
  const value = raw.replace(/^[ \t]+|[ \t]+$/g, "");
  if (value.startsWith('"')) {
    if (value.length < 2 || !/(^|[^\\])(\\\\)*"$/.test(value)) return null;
    let out = "";
    for (let i = 1; i < value.length - 1; i += 1) {
      const ch = value[i];
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
      const simple = {
        n: "\n", t: "\t", r: "\r", "\\": "\\", '"': '"', "/": "/",
        b: "\b", f: "\f", 0: "\0", " ": " ",
      };
      const mapped = esc === undefined ? undefined : simple[esc];
      if (mapped === undefined) return null;
      out += mapped;
    }
    return out;
  }
  if (value.startsWith("'")) {
    if (value.length < 2 || !value.endsWith("'")) return null;
    const inner = value.slice(1, -1);
    if (inner.replace(/''/g, "").includes("'")) return null;
    return inner.replace(/''/g, "'");
  }
  const comment = value.search(/(^|[ \t])#/);
  const text = (comment === -1 ? value : value.slice(0, comment)).replace(/[ \t]+$/, "");
  if (text === "" || /^[&*!%@`]/.test(text) || NON_STRING_PLAIN.test(text)) return null;
  return text;
}

/**
 * Byte-for-byte the loader's `readScalar`. Always reports the EXTENT of the
 * value so the caller resumes where YAML would; `value: null` means the text is
 * present but not exactly reproducible.
 */
export function readScalar(lines, index, raw) {
  const header = raw.replace(/^[ \t]+|[ \t]+$/g, "");
  if (!/^[>|]/.test(header)) {
    // A plain or flow scalar can CONTINUE on more-indented lines: YAML folds
    //     description: first
    //       second
    // into "first second", and `description: [` can open a sequence that closes
    // pages later. Reading only the first line returns "first" or "[" and calls
    // it exact. Both the continuation lines are consumed (so parsing resumes
    // where YAML would) and the value is refused (so nothing is guessed at).
    // A blank line does NOT end a plain scalar, and an indented comment is not
    // content. So scan past both, and treat this as a continuation only if some
    // indented, non-comment line actually follows. Stopping at the first blank
    // returned "first" for
    //     description: first
    //
    //       second
    // which YAML folds to "first second".
    let cursor = index + 1;
    let lastContent = index;
    while (cursor < lines.length) {
      const line = lines[cursor];
      if (line.trim() === "") { cursor += 1; continue; }
      if (!/^[ \t]/.test(line)) break;
      if (!line.trimStart().startsWith("#")) lastContent = cursor;
      cursor += 1;
    }
    // Resume AFTER the continuation, or at the next line when there was none;
    // blank and comment lines the outer loop skips on its own.
    if (lastContent > index) return { value: null, next: lastContent + 1 };
    return { value: readInlineScalar(header), next: index + 1 };
  }
  const reproducible = /^[>|]-?$/.test(header);
  const folded = header.startsWith(">");
  const chomped = header.endsWith("-");
  const content = [];
  let cursor = index + 1;
  for (; cursor < lines.length; cursor += 1) {
    const line = lines[cursor];
    if (line.trim() !== "" && !/^[ \t]/.test(line)) break;
    content.push(line);
  }
  while (content.length && content[content.length - 1].trim() === "") content.pop();
  if (!reproducible) return { value: null, next: cursor };
  if (!content.length) return { value: "", next: cursor };
  const base = content[0].length - content[0].trimStart().length;
  const parts = [];
  for (const line of content) {
    if (line.trim() === "") return { value: null, next: cursor };
    if (/\t/.test(line.slice(0, base + 1))) return { value: null, next: cursor };
    if (line.length - line.trimStart().length !== base) return { value: null, next: cursor };
    if (/\s$/.test(line)) return { value: null, next: cursor };
    parts.push(line.slice(base));
  }
  return { value: parts.join(folded ? " " : "\n") + (chomped ? "" : "\n"), next: cursor };
}


/**
 * Named for the value it returns, but it applies every rule the RUNTIME
 * applies. Anything the loader refuses and this accepts is an artifact the
 * build gate calls READY and production then silently drops.
 */
export function parseBundledSkillName(markdown, label) {
  const frontmatter = /^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/.exec(markdown);
  if (!frontmatter) fail(`${label} lacks YAML frontmatter`);
  const lines = frontmatter[1].split(/\r?\n/);
  let name = null;
  let description = null;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const key = plainTopLevelKey(line);
    if (!key) continue;
    if (EXECUTABLE_FRONTMATTER_KEYS.has(key.toLowerCase())) {
      fail(`${label} declares the executable frontmatter key ${JSON.stringify(key)}`);
    }
    const scalar = readScalar(lines, index, line.slice(line.indexOf(":") + 1));
    index = scalar.next - 1;
    // Same scoping as the loader: only the two values that get re-emitted
    // have to be reproducible.
    if (key === "name" && name === null) {
      if (scalar.value === null) fail(`${label} name is a YAML form this pipeline cannot reproduce`);
      name = scalar.value;
    }
    if (key === "description" && description === null) {
      if (scalar.value === null) fail(`${label} description is a YAML form this pipeline cannot reproduce`);
      description = scalar.value;
    }
  }
  if (name === null) fail(`${label} lacks a frontmatter name`);
  // The SAME name rule the loader applies. Without it this function accepted
  // `CON`, `probe.` and `bad/name` that production rejects — the directory
  // validator happens to catch them today, which makes the disagreement
  // invisible rather than absent.
  if (!isValidSkillName(name)) fail(`${label} declares an invalid skill name: ${name}`);
  // The runtime refuses a skill with no usable description, because that text
  // is the only thing the model reads when deciding the skill applies.
  if (!description) fail(`${label} lacks a usable frontmatter description`);
  return name;
}

export async function readSkill(sourceRoot, name) {
  if (!isValidSkillName(name)) fail(`invalid skill name: ${String(name)}`);
  const skillDir = path.join(sourceRoot, name);
  const skillFile = path.join(skillDir, "SKILL.md");
  await assertPlainDirectory(skillDir, `skill directory ${name}`);
  await assertPlainFile(skillFile, `SKILL.md for ${name}`, MAX_SKILL_FILE_BYTES);
  const content = await fs.readFile(skillFile, "utf8");
  const declaredName = parseSkillName(content, `SKILL.md for ${name}`);
  if (declaredName !== name) {
    fail(`SKILL.md frontmatter name ${JSON.stringify(declaredName)} does not match directory ${JSON.stringify(name)}`);
  }
  return { content, sourceFile: skillFile };
}

export async function readCuration(curationPath, sourceRoot) {
  await assertPlainFile(curationPath, "curation file");
  let entries;
  try {
    entries = JSON.parse(await fs.readFile(curationPath, "utf8"));
  } catch (error) {
    fail(`invalid curation JSON: ${error.message}`);
  }
  if (!Array.isArray(entries)) fail("curation JSON must be an array");

  const seen = new Set();
  for (const entry of entries) {
    if (!entry || typeof entry !== "object") fail("each curation entry must be an object");
    if (!isValidSkillName(entry.name)) fail(`invalid skill name: ${String(entry.name)}`);
    const key = entry.name.toLowerCase();
    if (seen.has(key)) fail(`case-only duplicate skill name: ${entry.name}`);
    seen.add(key);
    if (entry.tier !== "tenant-safe" && entry.tier !== "operator") {
      fail(`invalid tier for ${entry.name}: ${String(entry.tier)}`);
    }
    if (typeof entry.reason !== "string" || entry.reason.trim() === "") {
      fail(`curation entry ${entry.name} needs a reason`);
    }
  }

  await assertPlainDirectory(sourceRoot, "source directory");
  const sourceEntries = await fs.readdir(sourceRoot, { withFileTypes: true });
  const sourceNames = [];
  for (const entry of sourceEntries) {
    const skillFile = path.join(sourceRoot, entry.name, "SKILL.md");
    if (entry.isSymbolicLink()) {
      try {
        const fileStat = await fs.lstat(skillFile);
        if (fileStat.isFile()) fail(`source skill directory must not be a symlink: ${entry.name}`);
      } catch (error) {
        if (error instanceof Error && error.message.startsWith("source skill directory")) throw error;
        // A symlink without a SKILL.md is repository metadata, not a skill.
      }
      continue;
    }
    if (!entry.isDirectory()) continue;
    // The source root can carry repository metadata directories. A skill is a
    // direct child directory with a SKILL.md, not every directory in the root.
    try {
      const fileStat = await fs.lstat(skillFile);
      if (fileStat.isSymbolicLink()) fail(`source SKILL.md must not be a symlink: ${entry.name}`);
      if (fileStat.isFile()) sourceNames.push(entry.name);
    } catch (error) {
      if (error instanceof Error && error.message.startsWith("source SKILL.md")) throw error;
      // A non-skill directory has no SKILL.md and is out of curation scope.
    }
  }
  const sourceKeys = new Set(sourceNames.map((name) => name.toLowerCase()));
  if (sourceKeys.size !== sourceNames.length) fail("source contains case-only duplicate directories");
  if (sourceNames.length !== entries.length || [...seen].some((name) => !sourceKeys.has(name)) || [...sourceKeys].some((name) => !seen.has(name))) {
    fail("curation must classify every source skill directory exactly once");
  }

  const skills = [];
  for (const entry of entries) {
    const skill = await readSkill(sourceRoot, entry.name);
    skills.push({ ...entry, ...skill });
  }
  return skills.sort((left, right) => left.name.localeCompare(right.name));
}

export async function readJsonFile(filePath, label) {
  await assertPlainFile(filePath, label);
  try {
    return JSON.parse(await fs.readFile(filePath, "utf8"));
  } catch (error) {
    fail(`invalid ${label} JSON: ${error.message}`);
  }
}

function sortedEqual(left, right) {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

export async function validateBundleStructure(bundlePath) {
  await assertPlainDirectory(bundlePath, "bundle directory");
  const rootEntries = (await fs.readdir(bundlePath)).sort();
  if (!sortedEqual(rootEntries, [".claude-plugin", "manifest.json", "skills"])) {
    fail("bundle must contain only .claude-plugin, manifest.json, and skills");
  }

  const pluginDir = path.join(bundlePath, ".claude-plugin");
  const skillsDir = path.join(bundlePath, "skills");
  const pluginPath = path.join(pluginDir, "plugin.json");
  await assertPlainDirectory(pluginDir, ".claude-plugin directory");
  await assertPlainDirectory(skillsDir, "skills directory");
  if (!sortedEqual((await fs.readdir(pluginDir)).sort(), ["plugin.json"])) {
    fail(".claude-plugin must contain only plugin.json");
  }
  await assertPlainFile(pluginPath, "plugin.json", MAX_MANIFEST_BYTES);
  // KEY ALLOWLIST. The SDK mounts this file as a plugin manifest, and an
  // unknown key is not inert: `monitors` declares unsandboxed tasks armed at
  // session start. Hash inventories cannot catch this on their own, because a
  // manifest can be regenerated over the edited file.
  // ONE read, kept. Parsing here and hashing a SECOND read in verify.mjs is the
  // same window the loader had: a racing bundle can show safe bytes to the
  // policy check and different, manifest-matching bytes to the hash, and the
  // deployment gate then blesses an artifact production refuses. The buffers
  // inspected below are the ones returned for hashing.
  const contents = new Map();
  const pluginBuffer = await fs.readFile(pluginPath);
  contents.set(".claude-plugin/plugin.json", pluginBuffer);
  let plugin;
  try {
    plugin = JSON.parse(pluginBuffer.toString("utf8"));
  } catch (error) {
    fail(`invalid plugin.json JSON: ${error.message}`);
  }
  if (!plugin || typeof plugin !== "object" || Array.isArray(plugin)) fail("plugin.json must be a JSON object");
  const disallowed = Object.keys(plugin).filter((key) => !PLUGIN_KEYS.has(key));
  if (disallowed.length > 0) fail(`plugin.json has disallowed keys: ${disallowed.sort().join(", ")}`);
  // TYPES, not just key names. An allowed key holding the wrong type is still a
  // disagreement with the loader, which requires strings — and a bundle the
  // build gate blesses but the runtime refuses is the drift this pair exists to
  // prevent, just pointing the other way.
  if (typeof plugin.name !== "string" || typeof plugin.version !== "string") {
    fail("plugin.json name and version must be strings");
  }
  if (plugin.description !== undefined && typeof plugin.description !== "string") {
    fail("plugin.json description must be a string when present");
  }
  await assertPlainFile(path.join(bundlePath, "manifest.json"), "manifest.json", MAX_MANIFEST_BYTES);

  const names = (await fs.readdir(skillsDir)).sort();
  // ZERO skills is not a valid bundle. The runtime refuses it, so certifying
  // one here would recreate exactly the gate-versus-runtime disagreement this
  // pair of implementations exists to prevent. The builder cannot produce one,
  // but a hand-made directory can.
  if (names.length === 0) fail("bundle contains no skills");
  const seen = new Set();
  const files = [".claude-plugin/plugin.json"];
  for (const name of names) {
    if (!isValidSkillName(name)) fail(`invalid bundled skill name: ${name}`);
    const key = name.toLowerCase();
    if (seen.has(key)) fail(`case-only duplicate bundled skill name: ${name}`);
    seen.add(key);
    const skillDir = path.join(skillsDir, name);
    await assertPlainDirectory(skillDir, `bundled skill directory ${name}`);
    if (!sortedEqual((await fs.readdir(skillDir)).sort(), ["SKILL.md"])) {
      fail(`bundled skill ${name} must contain only SKILL.md`);
    }
    const skillPath = path.join(skillDir, "SKILL.md");
    await assertPlainFile(skillPath, `bundled SKILL.md for ${name}`, MAX_SKILL_FILE_BYTES);
    const skillBuffer = await fs.readFile(skillPath);
    contents.set(`skills/${name}/SKILL.md`, skillBuffer);
    const declaredName = parseBundledSkillName(skillBuffer.toString("utf8"), `bundled SKILL.md for ${name}`);
    if (declaredName !== name) fail(`bundled SKILL.md name ${JSON.stringify(declaredName)} does not match directory ${JSON.stringify(name)}`);
    files.push(`skills/${name}/SKILL.md`);
  }
  return { files: files.sort(), names: names.sort(), pluginPath, contents };
}
