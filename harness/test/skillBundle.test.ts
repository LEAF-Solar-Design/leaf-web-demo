import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { linkSync, mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import { materialiseVerifiedBundle, skillBundleAttachment, verifyBundle } from "../src/ports/impl/skillBundle.js";

const made: string[] = [];
const repo = resolve(import.meta.dirname, "../..");
const builder = join(repo, "tools", "skills-bundle", "build.mjs");
const offlineVerifier = join(repo, "tools", "skills-bundle", "verify.mjs");
const skillSource = "C:/Users/ehaug/.claude/skills";

function sha256(value: Buffer | string): string {
  return createHash("sha256").update(value).digest("hex");
}

function bundleDigest(files: Record<string, string>): string {
  return sha256(Object.keys(files).sort().map((path) => `${path}:${files[path]}`).join("\n"));
}

function buildBundle(): string {
  const parent = mkdtempSync(join(tmpdir(), "leaf-verified-bundle-"));
  made.push(parent);
  const output = join(parent, "bundle");
  // Bounded: vitest's per-test timeout cannot interrupt a SYNCHRONOUS child,
  // so a hung builder would hang the run forever rather than fail it.
  execFileSync(process.execPath, [builder, "--source", skillSource, "--tier", "tenant-safe", "--out", output], { stdio: "pipe", timeout: 60_000 });
  return output;
}

function manifest(path: string): { files: Record<string, string>; bundleDigest: string } {
  return JSON.parse(readFileSync(join(path, "manifest.json"), "utf8")) as { files: Record<string, string>; bundleDigest: string };
}

function refreshManifest(path: string): void {
  const current = manifest(path);
  const files: Record<string, string> = {};
  for (const relativePath of Object.keys(current.files)) {
    files[relativePath] = sha256(readFileSync(join(path, ...relativePath.split("/"))));
  }
  writeFileSync(join(path, "manifest.json"), `${JSON.stringify({ version: 1, tier: "tenant-safe", files, bundleDigest: bundleDigest(files) }, null, 2)}\n`);
}

function offlineAccepts(path: string): boolean {
  return spawnSync(process.execPath, [offlineVerifier, path], { stdio: "pipe", timeout: 60_000 }).status === 0;
}

afterEach(() => {
  for (const path of made.splice(0)) rmSync(path, { recursive: true, force: true });
});

// Each case below spawns the REAL builder (tools/skills-bundle/build.mjs) so
// "the verified shape" means the artifact the pipeline actually produces — the
// previous attempt used a hand-made fixture and hid a bug that way. A child
// process costs seconds under full-suite contention, so these get a realistic
// budget: vitest's 5s default is a statement about THIS harness, not about the
// code under test, and letting it fire would read as a security regression.
describe("verifyBundle", { timeout: 60_000 }, () => {
  it("accepts the real builder artifact and mounts a NORMALISED SNAPSHOT", () => {
    const path = buildBundle();
    const verified = verifyBundle(path);
    expect(verified.ok).toBe(true);
    if (!verified.ok) return;
    const attachment = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv);
    expect(attachment).toMatchObject({
      skills: verified.skills.map((skill) => skill.name), tier: "tenant-safe",
    });
    // The SDK must NOT be pointed at the tenant's directory: verifying bytes
    // and then mounting a path someone else can rewrite proves nothing (a
    // swap between the hash check and the SDK's read defeats even a pin).
    expect(attachment!.plugin.path).not.toBe(path);
    // ...and the snapshot's frontmatter is one WE authored: whatever the
    // source declared cannot survive a document we construct.
    const mounted = readFileSync(
      join(attachment!.plugin.path, "skills", verified.skills[0]!.name, "SKILL.md"), "utf8");
    expect(mounted.startsWith('---\nname: "')).toBe(true);
    expect(mounted).not.toMatch(/context|hooks|allowed-tools/i);
  });

  it("REFUSES to mount without a deployment digest pin", () => {
    // A self-consistent bundle proves consistency, not provenance: anyone who
    // can write the directory can regenerate the manifest and its digest.
    const path = buildBundle();
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: path, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("a post-verification swap cannot reach the SDK", () => {
    // The TOCTOU the snapshot exists for: mount, then rewrite the SOURCE.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const attachment = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv)!;
    const name = verified.skills[0]!.name;
    writeFileSync(join(path, "skills", name, "SKILL.md"),
      ["---", `name: ${name}`, "context: fork", "---", "pwned"].join("\n"));
    const mounted = readFileSync(join(attachment.plugin.path, "skills", name, "SKILL.md"), "utf8");
    expect(mounted).not.toContain("pwned");
    expect(mounted).not.toContain("fork");
  });

  it("materialises the VERIFIED bytes, not a fresh read of the source", () => {
    // The window a hash check cannot close on its own: verify, then copy. If
    // the copy re-opens the file, a writer between the two reads gets its bytes
    // mounted with the verified bundle's blessing. The mount must therefore be
    // written from the buffer that was hashed.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const name = verified.skills[0]!.name;
    writeFileSync(join(path, "skills", name, "SKILL.md"),
      ["---", `name: ${name}`, "context: fork", "---", "pwned"].join("\n"));
    const mount = materialiseVerifiedBundle(verified);
    expect(mount).not.toBeNull();
    const body = readFileSync(join(mount!, "skills", name, "SKILL.md"), "utf8");
    expect(body).not.toContain("pwned");
    expect(body).not.toContain("fork");
  });

  it("carries a FOLDED description through to the mount", () => {
    // Real curated skills write `description: >-` over several indented lines.
    // Reading only the text after the colon yields the literal ">-", and the
    // description is what the model reads to decide a skill is relevant — so
    // losing it disables the skill silently instead of loudly.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const standards = verified.skills.find((skill) => skill.name === "code-standards");
    expect(standards).toBeDefined();
    expect(standards!.description).not.toBe(">-");
    expect(standards!.description.length).toBeGreaterThan(40);
    expect(standards!.description).toContain("disciplined engineering workflow");
    const mount = materialiseVerifiedBundle(verified)!;
    expect(readFileSync(join(mount, "skills", "code-standards", "SKILL.md"), "utf8"))
      .toContain("disciplined engineering workflow");
  });

  it("mounts ONCE per configuration instead of once per turn", () => {
    // Both runners call this every turn. Re-verifying and re-copying each time
    // is a full bundle hash on the hot path plus an unbounded pile of temp
    // directories on a long-lived server.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const env = {
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv;
    const first = skillBundleAttachment(env);
    const second = skillBundleAttachment(env);
    expect(first).not.toBeNull();
    expect(second!.plugin.path).toBe(first!.plugin.path);
  });

  it("refuses a flipped byte in a SKILL.md", () => {
    const path = buildBundle();
    const file = join(path, "skills", "code-standards", "SKILL.md");
    writeFileSync(file, `${readFileSync(file, "utf8")}x`);
    expect(verifyBundle(path).ok).toBe(false);
  });

  it.each([
    ["root", (path: string) => writeFileSync(join(path, "payload.txt"), "x")],
    ["plugin", (path: string) => writeFileSync(join(path, ".claude-plugin", "payload.txt"), "x")],
    ["skill", (path: string) => writeFileSync(join(path, "skills", "code-standards", "payload.txt"), "x")],
    ["nested", (path: string) => { mkdirSync(join(path, "skills", "code-standards", "nested")); writeFileSync(join(path, "skills", "code-standards", "nested", "payload.txt"), "x"); }],
  ])("refuses an extra file at %s", (_where, mutate) => {
    const path = buildBundle();
    mutate(path);
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a deleted manifest entry", () => {
    const path = buildBundle();
    const current = manifest(path);
    delete current.files[Object.keys(current.files)[0]!];
    current.bundleDigest = bundleDigest(current.files);
    writeFileSync(join(path, "manifest.json"), JSON.stringify({ version: 1, tier: "tenant-safe", files: current.files, bundleDigest: current.bundleDigest }));
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a tampered bundle digest", () => {
    const path = buildBundle();
    const current = manifest(path);
    current.bundleDigest = "0".repeat(64);
    writeFileSync(join(path, "manifest.json"), JSON.stringify({ version: 1, tier: "tenant-safe", files: current.files, bundleDigest: current.bundleDigest }));
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a plugin monitor even when its hash inventory is otherwise valid", () => {
    const path = buildBundle();
    const pluginPath = join(path, ".claude-plugin", "plugin.json");
    writeFileSync(pluginPath, JSON.stringify({ ...JSON.parse(readFileSync(pluginPath, "utf8")), monitors: [{ command: "pwn" }] }));
    refreshManifest(path);
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a YAML-quoted context: fork key even when its hash inventory is valid", () => {
    const path = buildBundle();
    const file = join(path, "skills", "code-standards", "SKILL.md");
    writeFileSync(file, readFileSync(file, "utf8").replace("description:", '"context": fork\ndescription:'));
    refreshManifest(path);
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a symlink anywhere when the filesystem permits one", () => {
    const path = buildBundle();
    try { symlinkSync(join(path, "skills", "code-standards", "SKILL.md"), join(path, "skills", "code-standards", "link.md")); } catch { return; }
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("refuses a hardlink anywhere when the filesystem permits one", () => {
    const path = buildBundle();
    try { linkSync(join(path, "skills", "code-standards", "SKILL.md"), join(path, "skills", "code-standards", "link.md")); } catch { return; }
    expect(verifyBundle(path).ok).toBe(false);
  });

  it("enforces the optional deployment digest pin", () => {
    const path = buildBundle();
    const expected = manifest(path).bundleDigest;
    expect(verifyBundle(path, { expectedDigest: "0".repeat(64) }).ok).toBe(false);
    expect(verifyBundle(path, { expectedDigest: expected }).ok).toBe(true);
    expect(skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: path, LEAF_SKILLS_TIER: "tenant-safe", LEAF_SKILLS_BUNDLE_DIGEST: "0".repeat(64) } as NodeJS.ProcessEnv)).toBeNull();
  });
});

// The loader (TypeScript, runtime) and tools/skills-bundle/verify.mjs (the
// build and deploy gate) implement ONE rule set twice. Drift is silent and
// dangerous in both directions: a rule only the loader has makes CI bless an
// artifact production then refuses, and a rule only the verifier has is a
// runtime hole. So the corpus below is deliberately hostile, not just a valid
// bundle and a flipped byte — an earlier version of this test agreed on three
// trivial cases while the two sides disagreed on every interesting one.
describe("loader and offline verifier cross-check", { timeout: 120_000 }, () => {
  const corpus: Array<[string, (path: string) => void]> = [
    ["a genuine bundle", () => { }],
    ["a changed SKILL.md", (path) => writeFileSync(join(path, "skills", "code-standards", "SKILL.md"), "changed")],
    ["an extra file at the root", (path) => writeFileSync(join(path, "unexpected.txt"), "x")],
    ["an extra file beside a skill", (path) => writeFileSync(join(path, "skills", "code-standards", "payload.txt"), "x")],
    ["a nested payload directory", (path) => {
      mkdirSync(join(path, "skills", "code-standards", "nested"));
      writeFileSync(join(path, "skills", "code-standards", "nested", "payload.txt"), "x");
    }],
    ["a plugin monitor with a refreshed manifest", (path) => {
      const pluginPath = join(path, ".claude-plugin", "plugin.json");
      writeFileSync(pluginPath, JSON.stringify({ ...JSON.parse(readFileSync(pluginPath, "utf8")), monitors: [{ command: "pwn" }] }));
      refreshManifest(path);
    }],
    ["a bare context: fork frontmatter key", (path) => {
      const file = join(path, "skills", "code-standards", "SKILL.md");
      writeFileSync(file, readFileSync(file, "utf8").replace("description:", "context: fork\ndescription:"));
      refreshManifest(path);
    }],
    ["a YAML-quoted context key", (path) => {
      const file = join(path, "skills", "code-standards", "SKILL.md");
      writeFileSync(file, readFileSync(file, "utf8").replace("description:", '"context": fork\ndescription:'));
      refreshManifest(path);
    }],
    ["a plugin description of the wrong type", (path) => {
      const pluginPath = join(path, ".claude-plugin", "plugin.json");
      writeFileSync(pluginPath, JSON.stringify({ ...JSON.parse(readFileSync(pluginPath, "utf8")), description: {} }));
      refreshManifest(path);
    }],
    // Padded but otherwise VALID: the hashes, the file list and the digest all
    // still check out, so size is the only thing left that can reject it.
    ["an oversized manifest.json", (path) => {
      const current = manifest(path);
      writeFileSync(join(path, "manifest.json"), JSON.stringify({
        version: 1, tier: "tenant-safe", files: current.files,
        bundleDigest: current.bundleDigest, pad: "x".repeat(70_000),
      }));
    }],
    ["a tampered bundle digest", (path) => {
      const current = manifest(path);
      writeFileSync(join(path, "manifest.json"), JSON.stringify({ version: 1, tier: "tenant-safe", files: current.files, bundleDigest: "0".repeat(64) }));
    }],
    ["a deleted manifest entry", (path) => {
      const current = manifest(path);
      delete current.files[Object.keys(current.files)[0]!];
      writeFileSync(join(path, "manifest.json"), JSON.stringify({ version: 1, tier: "tenant-safe", files: current.files, bundleDigest: bundleDigest(current.files) }));
    }],
  ];

  it.each(corpus)("agree on %s", (_case, mutate) => {
    const path = buildBundle();
    mutate(path);
    expect(verifyBundle(path).ok).toBe(offlineAccepts(path));
  });
});
