import assert from "node:assert/strict";
import { mkdtemp, lstat, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { bundleDigest, parseBundledSkillName, sha256 } from "./common.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUILD = path.join(HERE, "build.mjs");
const VERIFY = path.join(HERE, "verify.mjs");

async function fixture() {
  const root = await mkdtemp(path.join(os.tmpdir(), "skills-bundle-"));
  const source = path.join(root, "source");
  await mkdir(source);
  return { root, source, curation: path.join(root, "curation.json"), out: path.join(root, "bundle") };
}

async function skill(source, name, body = "Useful skill.") {
  const directory = path.join(source, name);
  await mkdir(directory);
  await writeFile(path.join(directory, "SKILL.md"), `---\nname: ${name}\ndescription: fixture\n---\n${body}\n`);
}

async function skills(source, count) {
  for (let index = 0; index < count; index += 1) {
    await skill(source, `skill-${String(index).padStart(3, "0")}`);
  }
}

function entries(count, tier = "tenant-safe") {
  return Array.from({ length: count }, (_, index) => ({
    name: `skill-${String(index).padStart(3, "0")}`,
    tier,
    reason: "fixture",
  }));
}

async function curation(file, entries) {
  await writeFile(file, `${JSON.stringify(entries, null, 2)}\n`);
}

function run(script, args) {
  // Bounded: node:test cannot interrupt a synchronous child, so a hung
  // builder would hang the whole run instead of failing it.
  return spawnSync(process.execPath, [script, ...args], { encoding: "utf8", timeout: 60_000 });
}

// Re-hash a bundle in place so a tampering case cannot be caught by the hash
// inventory alone — the rule under test has to do the catching.
async function refreshManifest(bundlePath) {
  const manifestPath = path.join(bundlePath, "manifest.json");
  const current = JSON.parse(await readFile(manifestPath, "utf8"));
  const files = {};
  for (const relativePath of Object.keys(current.files)) {
    files[relativePath] = sha256(await readFile(path.join(bundlePath, ...relativePath.split("/"))));
  }
  await writeFile(manifestPath, `${JSON.stringify({ ...current, files, bundleDigest: bundleDigest(files) }, null, 2)}\n`);
}

async function assertNoLinks(directory) {
  for (const entry of await (await import("node:fs/promises")).readdir(directory, { withFileTypes: true })) {
    const item = path.join(directory, entry.name);
    const stat = await lstat(item);
    assert.equal(stat.isSymbolicLink(), false, `${item} must not be a symlink`);
    if (stat.isFile()) assert.equal(stat.nlink, 1, `${item} must not be hardlinked`);
    if (stat.isDirectory()) await assertNoLinks(item);
  }
}

test("build then verify accepts a copied tenant-safe bundle with no links", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
  assert.equal(built.status, 0, built.stderr);
  const verified = run(VERIFY, [work.out]);
  assert.equal(verified.status, 0, verified.stderr);
  assert.match(verified.stdout, /^READY:/);
  await assertNoLinks(work.out);
});

test("verify rejects one changed byte in a bundled SKILL.md", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  const bundled = path.join(work.out, "skills", "writing-aid", "SKILL.md");
  const before = await readFile(bundled, "utf8");
  await writeFile(bundled, `${before.slice(0, -1)}!`);
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: hash mismatch/);
});

test("verify exits nonzero when a built bundle contains an extra file", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  await writeFile(path.join(work.out, "extra.txt"), "smuggled\n");
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: bundle must contain only/);
});

// A hash inventory alone cannot catch either of the next two: whoever edits
// the file can regenerate the manifest over it. Both rules are duplicated in
// harness/src/ports/impl/skillBundle.ts, and skillBundle.test.ts cross-checks
// that the two sides agree.
test("verify exits nonzero for an unknown plugin.json key such as monitors", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  const pluginPath = path.join(work.out, ".claude-plugin", "plugin.json");
  const plugin = JSON.parse(await readFile(pluginPath, "utf8"));
  // `monitors` declares unsandboxed tasks armed at session start.
  await writeFile(pluginPath, JSON.stringify({ ...plugin, monitors: [{ command: "pwn" }] }));
  await refreshManifest(work.out);
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: plugin\.json has disallowed keys: monitors/);
});

// `context: fork` spawns a subagent, and neither disableSkillShellExecution
// nor disableAllHooks disables it. The quoted spelling is the same key to a
// YAML reader, so a check that reads only bare keys is not a check.
for (const [label, declaration] of [["bare", "context: fork"], ["quoted", '"context": fork']]) {
  test(`builder exits nonzero for a ${label} executable frontmatter key`, async (t) => {
    const work = await fixture();
    t.after(() => rm(work.root, { recursive: true, force: true }));
    await mkdir(path.join(work.source, "writing-aid"), { recursive: true });
    await writeFile(
      path.join(work.source, "writing-aid", "SKILL.md"),
      `---\nname: writing-aid\n${declaration}\ndescription: Useful skill.\n---\n\nBody.\n`,
    );
    await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
    const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
    assert.notEqual(built.status, 0);
    assert.match(built.stderr, /NOT-READY: bundled SKILL\.md for writing-aid declares the executable frontmatter key "context"/);
  });
}

test("verify exits nonzero when manifest tier disagrees with the build tier", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  const manifestPath = path.join(work.out, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  manifest.tier = "operator";
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: plugin.json leafTier does not match manifest tier/);
});

test("builder exits nonzero for a symlinked source skill directory", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  const target = path.join(work.root, "linked-target");
  await mkdir(target);
  await writeFile(path.join(target, "SKILL.md"), "---\nname: linked-skill\ndescription: fixture\n---\nUseful skill.\n");
  try {
    await symlink(target, path.join(work.source, "linked-skill"), "junction");
  } catch (error) {
    t.skip(`symlink creation denied: ${error.code ?? error.message}`);
    return;
  }
  await curation(work.curation, [{ name: "linked-skill", tier: "tenant-safe", reason: "fixture" }]);
  const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
  assert.notEqual(built.status, 0);
  assert.match(built.stderr, /NOT-READY: source skill directory must not be a symlink/);
});

test("builder exits nonzero for more than 250 curated skills", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skills(work.source, 251);
  await curation(work.curation, entries(251));
  const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
  assert.notEqual(built.status, 0);
  assert.match(built.stderr, /NOT-READY: curated tenant-safe bundle exceeds 250 skills/);
});

test("verify exits nonzero for more than 250 bundled skills", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skills(work.source, 250);
  await curation(work.curation, entries(250));
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  const extraName = "skill-250";
  const extraDir = path.join(work.out, "skills", extraName);
  await mkdir(extraDir);
  await writeFile(path.join(extraDir, "SKILL.md"), `---\nname: ${extraName}\ndescription: fixture\n---\nUseful skill.\n`);
  const manifestPath = path.join(work.out, "manifest.json");
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const extraPath = `skills/${extraName}/SKILL.md`;
  const { createHash } = await import("node:crypto");
  const digest = (value) => createHash("sha256").update(value).digest("hex");
  manifest.files[extraPath] = digest(await readFile(path.join(extraDir, "SKILL.md")));
  manifest.bundleDigest = digest(Object.keys(manifest.files).sort().map((name) => `${name}:${manifest.files[name]}`).join("\n"));
  await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: bundle exceeds 250 skills/);
});

test("verify exits nonzero for an oversized plugin manifest", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "writing-aid");
  await curation(work.curation, [{ name: "writing-aid", tier: "tenant-safe", reason: "fixture" }]);
  assert.equal(run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]).status, 0);
  await writeFile(path.join(work.out, ".claude-plugin", "plugin.json"), "x".repeat(64 * 1024 + 1));
  const verified = run(VERIFY, [work.out]);
  assert.notEqual(verified.status, 0);
  assert.match(verified.stderr, /NOT-READY: plugin.json exceeds 65536 bytes/);
});

for (const hostile of ["../../x", "CON", "probe."]) {
  test(`builder rejects hostile curation name ${JSON.stringify(hostile)}`, async (t) => {
    const work = await fixture();
    t.after(() => rm(work.root, { recursive: true, force: true }));
    await skill(work.source, "safe-skill");
    await curation(work.curation, [{ name: hostile, tier: "tenant-safe", reason: "hostile fixture" }]);
    const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
    assert.notEqual(built.status, 0);
    assert.match(built.stderr, /NOT-READY: invalid skill name/);
  });
}

test("builder rejects case-only duplicate curation names", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "probe");
  await curation(work.curation, [
    { name: "probe", tier: "tenant-safe", reason: "fixture" },
    { name: "Probe", tier: "tenant-safe", reason: "duplicate" },
  ]);
  const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
  assert.notEqual(built.status, 0);
  assert.match(built.stderr, /NOT-READY: case-only duplicate skill name/);
});

test("builder rejects an oversized SKILL.md", async (t) => {
  const work = await fixture();
  t.after(() => rm(work.root, { recursive: true, force: true }));
  await skill(work.source, "large-skill", "x".repeat(256 * 1024));
  await curation(work.curation, [{ name: "large-skill", tier: "tenant-safe", reason: "fixture" }]);
  const built = run(BUILD, ["--source", work.source, "--curation", work.curation, "--tier", "tenant-safe", "--out", work.out]);
  assert.notEqual(built.status, 0);
  assert.match(built.stderr, /NOT-READY: SKILL.md for large-skill exceeds 262144 bytes/);
});

// Called DIRECTLY, because the enclosing directory validator happens to reject
// these names first — which makes a disagreement with the loader invisible
// rather than absent. The exported function has to carry the rule itself.
test("parseBundledSkillName applies the loader's name rule", () => {
  const doc = (name) => `---
name: ${name}
description: real prose
---
body
`;
  for (const bad of ["CON", "probe.", "bad/name", "com1"]) {
    assert.throws(() => parseBundledSkillName(doc(bad), "fixture"),
      /invalid skill name/, `${bad} was accepted`);
  }
  assert.equal(parseBundledSkillName(doc("good-name"), "fixture"), "good-name");
});
