import assert from "node:assert/strict";
import { mkdtemp, lstat, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

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

async function curation(file, entries) {
  await writeFile(file, `${JSON.stringify(entries, null, 2)}\n`);
}

function run(script, args) {
  return spawnSync(process.execPath, [script, ...args], { encoding: "utf8" });
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
