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

export function fail(message) {
  throw new Error(message);
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
  await assertPlainFile(path.join(bundlePath, "manifest.json"), "manifest.json");

  const names = (await fs.readdir(skillsDir)).sort();
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
    const declaredName = parseSkillName(await fs.readFile(skillPath, "utf8"), `bundled SKILL.md for ${name}`);
    if (declaredName !== name) fail(`bundled SKILL.md name ${JSON.stringify(declaredName)} does not match directory ${JSON.stringify(name)}`);
    files.push(`skills/${name}/SKILL.md`);
  }
  return { files: files.sort(), names: names.sort(), pluginPath };
}
