import assert from "node:assert/strict";
import { mkdtemp, lstat, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { bundleDigest, parseBundledSkillMeta, sha256 } from "./common.mjs";

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
test("parseBundledSkillMeta applies the loader's name rule", () => {
  const doc = (name) => `---
name: ${name}
description: real prose
---
body
`;
  for (const bad of ["CON", "probe.", "bad/name", "com1"]) {
    assert.throws(() => parseBundledSkillMeta(doc(bad), "fixture"),
      /invalid skill name/, `${bad} was accepted`);
  }
  assert.deepEqual(parseBundledSkillMeta(doc("good-name"), "fixture"),
    { name: "good-name", description: "real prose" });
});


// --------------------------------------------------------------------------- //
// The frontmatter scalar corpus. This USED to live in the loader's test file
// against a byte-identical second copy of this reader. The loader no longer
// parses YAML at all — it reads the description out of the digest-covered
// manifest — so these cases belong to the one reader that is left, here,
// offline, where a refusal fails the build instead of a request.
// --------------------------------------------------------------------------- //

const CRLF = String.fromCharCode(13, 10);
const LF = String.fromCharCode(10);
const TAB = String.fromCharCode(9);
const BS = String.fromCharCode(92);
const Q = String.fromCharCode(34);

const fm = (...body) =>
  parseBundledSkillMeta(["---", ...body, "---", "prose"].join(CRLF) + CRLF, "fixture");
/** The reader refuses by throwing, so "cannot read this" is an assertion of that. */
const refuses = (label, ...body) =>
  assert.throws(() => fm(...body), /fixture/, `${label} was accepted`);

test("folds a >- description into one line", () => {
  assert.equal(fm("name: skill-a", "description: >-", "  first line", "  second line").description,
    "first line second line");
});

test("keeps the line breaks of a |- description", () => {
  assert.equal(fm("name: skill-a", "description: |-", "  first line", "  second line").description,
    "first line" + LF + "second line");
});

test("reproduces chomping exactly: strip drops the trailing newline, clip keeps one", () => {
  assert.equal(fm("name: skill-a", "description: >-", "  text").description, "text");
  assert.equal(fm("name: skill-a", "description: >", "  text").description, "text" + LF);
  assert.equal(fm("name: skill-a", "description: |", "  a", "  b").description, "a" + LF + "b" + LF);
});

test("REFUSES text it would have to alter to read", () => {
  // Each of these is content YAML keeps and a trim() would quietly destroy.
  refuses("tab indent", "name: skill-a", "description: >-", `${TAB}text`);
  refuses("trailing spaces", "name: skill-a", "description: >-", "  text   ");
  refuses("interior blank", "name: skill-a", "description: >-", "  one", "", "  two");
  refuses("whitespace-only interior", "name: skill-a", "description: >-", "  one", "    ", "  two");
});

test("allows the ordinary blank line between a block and the next key", () => {
  // Trailing blanks are chomped by YAML, so refusing them would reject
  // perfectly normal formatting and break the real corpus.
  const parsed = fm("name: skill-a", "description: >-", "  text", "", "compatibility: claude-code");
  assert.equal(parsed.description, "text");
  assert.equal(parsed.name, "skill-a");
});

test("DECODES an inline scalar rather than stripping its quotes", () => {
  // A double-quoted \n is a newline to YAML and a backslash-n to a naive strip;
  // a doubled quote is one apostrophe; ` #` starts a comment.
  assert.equal(fm("name: skill-a", `description: "one${BS}ntwo"`).description, "one" + LF + "two");
  assert.equal(fm("name: skill-a", "description: 'it''s useful'").description, "it's useful");
  assert.equal(fm("name: skill-a", "description: useful # note").description, "useful");
  assert.equal(fm("name: skill-a", `description: "tab${BS}there"`).description, "tab" + TAB + "here");
});

test("REFUSES a scalar that is not one string", () => {
  // An unescaped interior quote is not one scalar; a naive parity check on the
  // closing quote accepts it and rewrites the value.
  refuses("interior quote", "name: skill-a", `description: ${Q}a${Q}b${Q}`);
  // ...and these are not strings at all to YAML. The near-misses are the point:
  // each is a number or a keyword to a YAML reader, so emitting it as the text
  // after the colon would say something the source did not.
  for (const value of ["false", "null", "~", "3.14", "0x1f", "[a, b]", "{a: b}", "yes",
                       ".5", "+.inf", "0b1010", "1_000", "1:30", "12:34:56", "Null", "TRUE"]) {
    refuses(value, "name: skill-a", `description: ${value}`);
  }
  // A bare date is a TIMESTAMP to js-yaml's default schema, not the text.
  refuses("bare date", "name: skill-a", "description: 2026-07-29");
  refuses("timestamp", "name: skill-a", "description: 2026-07-29T10:30:00Z");
  refuses("spaced timestamp", "name: skill-a", "description: 2026-07-29 10:30:00");
  // ...but date-LED PROSE is a string, and refusing it would refuse the whole
  // bundle over an ordinary description.
  for (const prose of ["2026-07-29 release notes helper", "2026-07-29 to 2026-08-01",
                       "2026-07-29TICKET helper"]) {
    assert.equal(fm("name: skill-a", `description: ${prose}`).description, prose, prose);
  }
  // ...and prose that merely contains a colon or a comma is obviously fine.
  assert.equal(fm("name: skill-a", "description: Use when: drafting, editing").description,
    "Use when: drafting, editing");
});

test("REFUSES a plain scalar that continues on the next line", () => {
  // YAML folds a more-indented continuation into the value, so
  //   description: first
  //     second
  // is "first second". Reading only the first line returns "first" and calls it
  // exact. A flow sequence opened with `[` and closed later is the same problem
  // wearing a different hat.
  refuses("indented continuation", "name: skill-a", "description: first", "  second");
  // A BLANK LINE does not end a plain scalar, so this folds to "first second"
  // too. Stopping at the blank returned "first" and called it exact.
  refuses("blank then continuation", "name: skill-a", "description: first", "", "  second");
  refuses("flow seq across a blank", "name: skill-a", "description: [", "", "  a, b]");
  refuses("flow seq", "name: skill-a", "description: [", "  a, b]");
  // ...but an indented COMMENT is not content, and neither is a trailing blank.
  assert.equal(fm("name: skill-a", "description: real prose", "  # just a comment").description,
    "real prose");
  assert.equal(fm("name: skill-a", "description: real prose", "", "other: x").description,
    "real prose");
  // ...and the continuation is CONSUMED, so a key below it is still inspected.
  refuses("key below a continuation", "name: skill-a", "description: real prose",
    "some-key: first", "  continued", "context: fork");
});

test("uses YAML whitespace, not JavaScript whitespace, to find a comment", () => {
  // A non-breaking space is not a YAML separator, so ` #` after one is NOT a
  // comment. Cutting there would silently shorten a real description.
  const NBSP = String.fromCharCode(160);
  assert.equal(fm("name: skill-a", `description: keep${NBSP}#this`).description,
    `keep${NBSP}#this`);
  assert.equal(fm("name: skill-a", "description: cut here # not this").description, "cut here");
});

test("only requires exactness from the values it re-emits", () => {
  // `name` and `description` are rewritten into the mounted document; nothing
  // else is. Refusing a skill because an unrelated key holds `false` would
  // reject a third of the real corpus and protect nothing.
  const parsed = fm("name: skill-a", "description: real prose", "some-flag: false",
                    "version: 3.14", "tags: [a, b]");
  assert.equal(parsed.name, "skill-a");
  assert.equal(parsed.description, "real prose");
});

test("REFUSES an inline scalar it cannot decode", () => {
  refuses("unterminated double quote", "name: skill-a", `description: ${Q}unterminated`);
  refuses("unterminated single quote", "name: skill-a", "description: 'unterminated");
  refuses("bad escape", "name: skill-a", `description: ${Q}bad ${BS}q escape${Q}`);
  refuses("short unicode escape", "name: skill-a", `description: ${Q}short ${BS}u12 unicode${Q}`);
  refuses("anchor reference", "name: skill-a", "description: *anchor-reference");
});

test("REFUSES a skill with no usable description", () => {
  // The description is the only text the model reads when deciding a skill
  // applies, so an empty one mounts a skill that is never chosen — silently
  // useless, which is the exact failure this pipeline exists to prevent.
  refuses("empty block", "name: skill-a", "description: >-");
  refuses("empty value", "name: skill-a", "description:");
  refuses("absent", "name: skill-a");
});

test("REFUSES a block form it cannot reproduce exactly", () => {
  // Each of these means something specific that folding would quietly rewrite:
  // an explicit indentation indicator makes leading spaces content, and `+`
  // keeps trailing blank lines on purpose.
  for (const header of [">2-", ">-2", "|+", "|-4", ">+", "|2"]) {
    refuses(header, "name: skill-a", `description: ${header}`, "  text");
  }
});

test("REFUSES a folded block whose lines are not evenly indented", () => {
  // YAML preserves the deeper indentation as structure; folding it away changes
  // the text, so the bundle is refused instead.
  refuses("uneven indent", "name: skill-a", "description: >-", "  even", "    deeper");
});

test("RESUMES after a block scalar, so a later executable key is still caught", () => {
  // The guard that makes this work is the break on a dedented line. Without it
  // the reader swallows `context: fork` as block content and the build ships a
  // skill whose source declares a subagent fork.
  refuses("context after block", "name: skill-a", "description: >-", "  text", "context: fork");
  refuses("hooks after block", "name: skill-a", "description: >-", "  text", "hooks: [x]");
  // ...and a harmless key after a block is still read normally.
  assert.equal(
    fm("name: skill-a", "description: >-", "  text", "compatibility: claude-code").name,
    "skill-a");
});

test("REFUSES a description longer than the loader will accept", () => {
  // The loader bounds a manifest description; if the build did not bound the
  // same text, it would produce a bundle production refuses.
  refuses("over-long", "name: skill-a", `description: ${"x".repeat(9000)}`);
});
