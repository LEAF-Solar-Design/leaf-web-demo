import { createHash } from "node:crypto";
import { execFileSync, spawnSync } from "node:child_process";
import { cpSync, existsSync, linkSync, mkdtempSync, mkdirSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterAll, afterEach, describe, expect, it } from "vitest";

import { materialiseVerifiedBundle, parseSkillFrontmatter, sanitiseLogText, skillBundleAttachment, verifyBundle } from "../src/ports/impl/skillBundle.js";

const made: string[] = [];
const templates: string[] = [];
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

// The builder is a CHILD PROCESS and this file wants a fresh bundle per case.
// Spawning one each time made this file the heaviest CPU consumer in the suite
// and pushed an unrelated 5s test over its budget. So the real artifact is
// built ONCE and copied per case: every case still gets its own mutable tree,
// byte-identical to the pipeline's output, for the cost of a file copy.
let template: string | null = null;

function buildBundle(): string {
  if (template === null) {
    const home = mkdtempSync(join(tmpdir(), "leaf-bundle-template-"));
    templates.push(home);
    const built = join(home, "bundle");
    // Bounded: vitest's per-test timeout cannot interrupt a SYNCHRONOUS child,
    // so a hung builder would hang the run forever rather than fail it.
    execFileSync(process.execPath,
      [builder, "--source", skillSource, "--tier", "tenant-safe", "--out", built],
      { stdio: "pipe", timeout: 60_000 });
    template = built;
  }
  const parent = mkdtempSync(join(tmpdir(), "leaf-verified-bundle-"));
  made.push(parent);
  const output = join(parent, "bundle");
  cpSync(template, output, { recursive: true });
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

afterAll(() => {
  for (const path of templates.splice(0)) rmSync(path, { recursive: true, force: true });
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

  it("RETRIES a refusal, so repairing a bundle in place does not need a restart", () => {
    // Caching a refusal is the tempting half of the memo and the wrong half: an
    // operator who fixes the bundle under the same path, tier and pin would
    // otherwise see nothing change until the process restarts.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const env = {
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv;
    const file = join(path, "skills", "code-standards", "SKILL.md");
    const good = readFileSync(file);

    writeFileSync(file, "broken");
    expect(skillBundleAttachment(env)).toBeNull();

    writeFileSync(file, good);
    expect(skillBundleAttachment(env)).not.toBeNull();
  });

  it("SAYS WHY it refused, because no-skills looks like no-skills-configured", () => {
    // A refusal removes every skill. Without a reason in the log, a tampered
    // byte is indistinguishable from a deployment that never configured a
    // bundle, and nobody goes looking.
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const said: string[] = [];
    const original = console.error;
    console.error = (...args: unknown[]) => { said.push(args.join(" ")); };
    try {
      skillBundleAttachment({
        LEAF_SKILLS_BUNDLE_PATH: path,
        LEAF_SKILLS_TIER: "tenant-safe",
        LEAF_SKILLS_BUNDLE_DIGEST: "0".repeat(64),
      } as NodeJS.ProcessEnv);
    } finally {
      console.error = original;
    }
    expect(said.join(" ")).toContain("deployment pin");
    expect(said.join(" ")).toContain(path);
  });

  it("ESCAPES control characters before putting a tenant filename in a log", () => {
    // Refusal reasons quote filenames from a tenant-writable directory. Written
    // raw, a crafted name can move the cursor or forge a line in a log that
    // people and parsers both read. Tested on the escaping itself rather than
    // through a real file, because Windows refuses to create such a name at all
    // and the test would pass by never running.
    const ESC = String.fromCharCode(27);
    const BEL = String.fromCharCode(7);
    const dirty = `skills/${ESC}[2J${BEL}wiped/SKILL.md: hash mismatch`;
    const clean = sanitiseLogText(dirty);
    expect(clean).not.toContain(ESC);
    expect(clean).not.toContain(BEL);
    expect(clean).toContain("\\x1b");
    expect(clean).toContain("hash mismatch");
    // ...and it cannot be grown without bound by a name that changes every turn.
    expect(sanitiseLogText("x".repeat(5000)).length).toBeLessThanOrEqual(300);
    // Not just C0/C1: U+2028 and U+2029 are LINE separators to a JavaScript or
    // JSON log reader, and the bidi overrides reorder what a human sees.
    for (const code of [0x2028, 0x2029, 0x202e, 0x2066, 0x200f]) {
      const escaped = sanitiseLogText(`before${String.fromCharCode(code)}after`);
      expect(escaped).not.toContain(String.fromCharCode(code));
      expect(escaped).toContain(code.toString(16));
    }
    // Truncating AFTER expansion can cut an escape in half and hand a parser a
    // dangling `\\x`; truncating the input first keeps every escape whole.
    const nearLimit = "x".repeat(298) + String.fromCharCode(27) + "tail";
    expect(sanitiseLogText(nearLimit).endsWith(String.fromCharCode(92) + "x")).toBe(false);
  });

  it("bounds the mount cache, and does NOT delete the snapshot it evicts", () => {
    const path = buildBundle();
    const verified = verifyBundle(path);
    if (!verified.ok) throw new Error("fixture did not verify");
    const first = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv)!;
    // Distinct keys, each a real mount, enough to push the first one out.
    for (let extra = 0; extra < 6; extra += 1) {
      const other = buildBundle();
      const otherVerified = verifyBundle(other);
      if (!otherVerified.ok) throw new Error("fixture did not verify");
      skillBundleAttachment({
        LEAF_SKILLS_BUNDLE_PATH: other,
        LEAF_SKILLS_TIER: "tenant-safe",
        LEAF_SKILLS_BUNDLE_DIGEST: otherVerified.digest,
      } as NodeJS.ProcessEnv);
    }
    // The ENTRY is gone — asking again re-verifies and mounts a fresh snapshot.
    const again = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: path,
      LEAF_SKILLS_TIER: "tenant-safe",
      LEAF_SKILLS_BUNDLE_DIGEST: verified.digest,
    } as NodeJS.ProcessEnv)!;
    expect(again.plugin.path).not.toBe(first.plugin.path);
    // ...but the evicted DIRECTORY survives, because an in-flight sdk.query may
    // still be reading it. Removing it under a live turn would trade a bounded
    // map for a broken session; exit cleanup owns it instead.
    expect(existsSync(first.plugin.path)).toBe(true);
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

  it("enforces the required deployment digest pin", () => {
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
    // A bundle with no skills: the builder cannot make one, a hand-made
    // directory can, and the runtime refuses it. The gate has to agree.
    ["a bundle with no skills at all", (path) => {
      for (const name of readdirSync(join(path, "skills"))) {
        rmSync(join(path, "skills", name), { recursive: true, force: true });
      }
      writeFileSync(join(path, "manifest.json"), JSON.stringify({
        version: 1, tier: "tenant-safe",
        files: { ".claude-plugin/plugin.json": sha256(readFileSync(join(path, ".claude-plugin", "plugin.json"))) },
        bundleDigest: bundleDigest({ ".claude-plugin/plugin.json": sha256(readFileSync(join(path, ".claude-plugin", "plugin.json"))) }),
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

// CRLF throughout: the real curated skills are CRLF, and a reader that only
// works on LF would pass every hand-written LF fixture and fail in production.
describe("frontmatter block scalars", () => {
  const CRLF = String.fromCharCode(13, 10);
  const fm = (...body: string[]) =>
    parseSkillFrontmatter(["---", ...body, "---", "prose"].join(CRLF) + CRLF);

  it("folds a >- description into one line", () => {
    expect(fm("name: skill-a", "description: >-", "  first line", "  second line")?.description)
      .toBe("first line second line");
  });

  it("keeps the line breaks of a |- description", () => {
    expect(fm("name: skill-a", "description: |-", "  first line", "  second line")?.description)
      .toBe("first line" + String.fromCharCode(10) + "second line");
  });

  it("reproduces chomping exactly: strip drops the trailing newline, clip keeps one", () => {
    const LF = String.fromCharCode(10);
    expect(fm("name: skill-a", "description: >-", "  text")?.description).toBe("text");
    expect(fm("name: skill-a", "description: >", "  text")?.description).toBe("text" + LF);
    expect(fm("name: skill-a", "description: |", "  a", "  b")?.description).toBe("a" + LF + "b" + LF);
  });

  it("REFUSES text it would have to alter to read", () => {
    // Each of these is content YAML keeps and a trim() would quietly destroy.
    const TAB = String.fromCharCode(9);
    expect(fm("name: skill-a", "description: >-", `${TAB}text`)).toBeNull();       // tab indent
    expect(fm("name: skill-a", "description: >-", "  text   ")).toBeNull();        // trailing spaces
    expect(fm("name: skill-a", "description: >-", "  one", "", "  two")).toBeNull(); // interior blank
    expect(fm("name: skill-a", "description: >-", "  one", "    ", "  two")).toBeNull();
  });

  it("allows the ordinary blank line between a block and the next key", () => {
    // Trailing blanks are chomped by YAML, so refusing them would reject
    // perfectly normal formatting and break the real corpus.
    const parsed = fm("name: skill-a", "description: >-", "  text", "", "compatibility: claude-code");
    expect(parsed?.description).toBe("text");
    expect(parsed?.name).toBe("skill-a");
  });

  it("DECODES an inline scalar rather than stripping its quotes", () => {
    const LF = String.fromCharCode(10);
    const BS = String.fromCharCode(92);
    // A double-quoted \n is a newline to YAML and a backslash-n to a naive
    // strip; a doubled quote is one apostrophe; ` #` starts a comment.
    expect(fm("name: skill-a", `description: "one${BS}ntwo"`)?.description).toBe("one" + LF + "two");
    expect(fm("name: skill-a", "description: 'it''s useful'")?.description).toBe("it's useful");
    expect(fm("name: skill-a", "description: useful # note")?.description).toBe("useful");
    expect(fm("name: skill-a", `description: "tab${BS}there"`)?.description)
      .toBe("tab" + String.fromCharCode(9) + "here");
  });

  it("REFUSES a scalar that is not one string", () => {
    const Q = String.fromCharCode(34);
    // An unescaped interior quote is not one scalar; a naive parity check on the
    // closing quote accepts it and rewrites the value.
    expect(fm("name: skill-a", `description: ${Q}a${Q}b${Q}`)).toBeNull();
    // ...and these are not strings at all to YAML. Emitting them as the text
    // between the colon and the newline would say something the source did not.
    // The near-misses are the point: each of these is a number or a keyword to a
    // YAML reader, so emitting it as the text after the colon would say
    // something the source did not.
    for (const value of ["false", "null", "~", "3.14", "0x1f", "[a, b]", "{a: b}", "yes",
                         ".5", "+.inf", "0b1010", "1_000", "1:30", "12:34:56", "Null", "TRUE"]) {
      expect(fm("name: skill-a", `description: ${value}`), value).toBeNull();
    }
    // ...but a date is a STRING under the schema these readers use, and prose
    // that merely contains a colon or a dash is obviously fine.
    expect(fm("name: skill-a", "description: 2026-07-29")?.description).toBe("2026-07-29");
    expect(fm("name: skill-a", "description: Use when: drafting, editing")?.description)
      .toBe("Use when: drafting, editing");
  });

  it("uses YAML whitespace, not JavaScript whitespace, to find a comment", () => {
    // A non-breaking space is not a YAML separator, so ` #` after one is NOT a
    // comment. Cutting there would silently shorten a real description.
    const NBSP = String.fromCharCode(160);
    expect(fm("name: skill-a", `description: keep${NBSP}#this`)?.description)
      .toBe(`keep${NBSP}#this`);
    expect(fm("name: skill-a", "description: cut here # not this")?.description).toBe("cut here");
  });

  it("only requires exactness from the values it re-emits", () => {
    // `name` and `description` are rewritten into the mounted document; nothing
    // else is. Refusing a skill because an unrelated key holds `false` would
    // reject a third of the real corpus and protect nothing.
    const parsed = fm("name: skill-a", "description: real prose", "some-flag: false",
                      "version: 3.14", "tags: [a, b]");
    expect(parsed?.name).toBe("skill-a");
    expect(parsed?.description).toBe("real prose");
  });

  it("REFUSES an inline scalar it cannot decode", () => {
    const BS = String.fromCharCode(92);
    expect(fm("name: skill-a", `description: "unterminated`)).toBeNull();
    expect(fm("name: skill-a", "description: 'unterminated")).toBeNull();
    expect(fm("name: skill-a", `description: "bad ${BS}q escape"`)).toBeNull();
    expect(fm("name: skill-a", `description: "short ${BS}u12 unicode"`)).toBeNull();
    expect(fm("name: skill-a", "description: *anchor-reference")).toBeNull();
  });

  it("REFUSES a skill with no usable description", () => {
    // The description is the only text the model reads when deciding a skill
    // applies, so an empty one mounts a skill that is never chosen — silently
    // useless, which is the exact failure this module exists to prevent.
    expect(fm("name: skill-a", "description: >-")).toBeNull();
    expect(fm("name: skill-a", "description:")).toBeNull();
    expect(fm("name: skill-a")).toBeNull();
  });

  it("REFUSES a block form it cannot reproduce exactly", () => {
    // Each of these means something specific that folding would quietly
    // rewrite: an explicit indentation indicator makes leading spaces content,
    // and `+` keeps trailing blank lines on purpose.
    for (const header of [">2-", ">-2", "|+", "|-4", ">+", "|2"]) {
      expect(fm("name: skill-a", `description: ${header}`, "  text")).toBeNull();
    }
  });

  it("REFUSES a folded block whose lines are not evenly indented", () => {
    // YAML preserves the deeper indentation as structure; folding it away
    // changes the text, so the bundle is refused instead.
    expect(fm("name: skill-a", "description: >-", "  even", "    deeper")).toBeNull();
  });

  it("RESUMES after a block scalar, so a later executable key is still caught", () => {
    // The guard that makes this work is the break on a dedented line. Without
    // it the parser swallows `context: fork` as block content and mounts a
    // skill that forks a subagent.
    expect(fm("name: skill-a", "description: >-", "  text", "context: fork")).toBeNull();
    expect(fm("name: skill-a", "description: >-", "  text", "hooks: [x]")).toBeNull();
    // ...and a harmless key after a block is still read normally.
    expect(fm("name: skill-a", "description: >-", "  text", "compatibility: claude-code")?.name)
      .toBe("skill-a");
  });
});
