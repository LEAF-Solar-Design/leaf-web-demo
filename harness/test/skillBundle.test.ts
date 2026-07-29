// Curated skill bundle: off unless mounted, explicit allowlist, disk-derived.
//
// These pin the three findings the Wave-0 spike settled, each of which is a
// property a future edit could silently undo:
//   1. `skills` is never 'all' (that would enable the CLI's bundled developer
//      skills — run/review/security-review — in a tenant session);
//   2. the allowlist comes from what is ON DISK, so it cannot drift from the
//      bundle's actual contents;
//   3. an empty/absent bundle attaches NOTHING, rather than enabling the Skill
//      tool with nothing behind it.
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  MAX_SKILL_FILE_BYTES,
  discoverSkills,
  readBundleTier,
  isValidSkillName,
  parseSkillFrontmatter,
  skillBundleAttachment,
} from "../src/ports/impl/skillBundle.js";

const made: string[] = [];

function bundleWith(
  skills: Array<{ name: string; description?: string; body?: string }>,
  tier: string | null = "tenant-safe",
): string {
  const root = mkdtempSync(join(tmpdir(), "leaf-skills-"));
  made.push(root);
  mkdirSync(join(root, ".claude-plugin"), { recursive: true });
  writeFileSync(join(root, ".claude-plugin", "plugin.json"),
    JSON.stringify({ name: "leaf-test", version: "0.0.1", ...(tier ? { leafTier: tier } : {}) }));
  for (const s of skills) {
    const dir = join(root, "skills", s.name);
    mkdirSync(dir, { recursive: true });
    const fm = `---\nname: ${s.name}\ndescription: ${s.description ?? "d"}\n---\n\n${s.body ?? "body"}\n`;
    writeFileSync(join(dir, "SKILL.md"), fm);
  }
  return root;
}

afterEach(() => {
  for (const dir of made.splice(0)) {
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* best effort */ }
  }
});

describe("parseSkillFrontmatter", () => {
  it("reads name and description", () => {
    expect(parseSkillFrontmatter("---\nname: a\ndescription: b\n---\nbody"))
      .toEqual({ name: "a", description: "b" });
  });

  it("strips surrounding quotes", () => {
    expect(parseSkillFrontmatter(`---\nname: "a"\ndescription: 'b'\n---\n`))
      .toEqual({ name: "a", description: "b" });
  });

  it("is null without frontmatter or without a name", () => {
    expect(parseSkillFrontmatter("no frontmatter")).toBeNull();
    expect(parseSkillFrontmatter("---\ndescription: b\n---\n")).toBeNull();
    expect(parseSkillFrontmatter("")).toBeNull();
  });
});

describe("discoverSkills", () => {
  it("finds every skill in the bundle, sorted", () => {
    const root = bundleWith([{ name: "zed" }, { name: "alpha" }]);
    expect(discoverSkills(root).map((s) => s.name)).toEqual(["alpha", "zed"]);
  });

  it("skips directories without a SKILL.md and unparseable ones", () => {
    const root = bundleWith([{ name: "good" }]);
    mkdirSync(join(root, "skills", "empty-dir"), { recursive: true });
    const broken = join(root, "skills", "broken");
    mkdirSync(broken, { recursive: true });
    writeFileSync(join(broken, "SKILL.md"), "no frontmatter here");
    expect(discoverSkills(root).map((s) => s.name)).toEqual(["good"]);
  });

  it("is empty for a missing bundle rather than throwing", () => {
    expect(discoverSkills(join(tmpdir(), "definitely-not-here-leaf"))).toEqual([]);
  });
});

describe("skillBundleAttachment", () => {
  it("is OFF when unconfigured — the session runs exactly as today", () => {
    expect(skillBundleAttachment({} as NodeJS.ProcessEnv)).toBeNull();
    expect(skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: "  " } as NodeJS.ProcessEnv)).toBeNull();
    // A path without a tier is also unconfigured — both halves are required.
    const some = bundleWith([{ name: "probe" }]);
    expect(skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: some } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("is OFF when the configured path does not exist", () => {
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: join(tmpdir(), "leaf-absent-bundle"),
      LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("is OFF for an empty bundle — never enable the Skill tool with nothing behind it", () => {
    const root = bundleWith([]);
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("emits a local plugin with MCP discovery skipped (the harness owns MCP)", () => {
    const root = bundleWith([{ name: "probe" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "tenant-safe" } as NodeJS.ProcessEnv);
    expect(att?.plugin).toEqual({ type: "local", path: root, skipMcpDiscovery: true });
  });

  it("ALWAYS emits an explicit allowlist, never 'all'", () => {
    // 'all' would also enable the CLI's bundled developer skills (run, review,
    // security-review) inside a tenant session. This is the guard.
    const root = bundleWith([{ name: "one" }, { name: "two" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "tenant-safe" } as NodeJS.ProcessEnv);
    expect(Array.isArray(att?.skills)).toBe(true);
    expect(att?.skills).not.toBe("all");
    expect(att?.skills).toEqual(["one", "two"]);
  });

  it("derives the allowlist from disk, so it cannot drift from the bundle", () => {
    const root = bundleWith([{ name: "only-this-one" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "tenant-safe" } as NodeJS.ProcessEnv);
    expect(att?.skills).toEqual(["only-this-one"]);
  });

  it("mounts ONE bundle path — the tenant/operator split is a disk boundary", () => {
    // `skills` is a context filter, not a sandbox: unlisted skills stay on disk
    // and are readable. So two tiers must be two DIRECTORIES, and the
    // attachment must carry exactly the one it was pointed at.
    const tenant = bundleWith([{ name: "tenant-safe-skill" }]);
    const operator = bundleWith([{ name: "tenant-safe-skill" }, { name: "operator-only-skill" }], "operator");
    const tenantAtt = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: tenant, LEAF_SKILLS_TIER: "tenant-safe" } as NodeJS.ProcessEnv);
    const operatorAtt = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: operator, LEAF_SKILLS_TIER: "operator" } as NodeJS.ProcessEnv);
    expect(tenantAtt?.skills).toEqual(["tenant-safe-skill"]);
    expect(tenantAtt?.plugin.path).toBe(tenant);
    expect(operatorAtt?.skills).toContain("operator-only-skill");
    expect(operatorAtt?.plugin.path).toBe(operator);
  });
});

describe("hostile input (review findings)", () => {
  // The reviewer got BOTH of these accepted by the first version. The SDK
  // expands `skills` names into comma-delimited tool patterns, so `safe),Bash`
  // is not cosmetic — it can widen the allowlist the money-gate relies on.
  const HOSTILE = [
    "../../operator",
    "safe),Bash",
    "a b",
    "name;rm -rf /",
    "with/slash",
    "with\backslash",
    "",
    ".",
    "..",
    "-leading-dash",
    "x".repeat(200),
  ];

  it("refuses hostile skill names outright", () => {
    for (const name of HOSTILE) {
      expect(isValidSkillName(name), `accepted ${JSON.stringify(name)}`).toBe(false);
    }
    expect(isValidSkillName("orwell-writing")).toBe(true);
    expect(isValidSkillName("leaf_fixture.probe-2")).toBe(true);
  });

  it("never lets a hostile name reach the allowlist", () => {
    const root = bundleWith([{ name: "good" }]);
    // Hand-plant a directory whose frontmatter name is hostile.
    const evil = join(root, "skills", "evil");
    mkdirSync(evil, { recursive: true });
    writeFileSync(join(evil, "SKILL.md"), "---\nname: safe),Bash\ndescription: x\n---\n");
    const att = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv);
    expect(att?.skills).toEqual(["good"]);
    for (const name of att?.skills ?? []) expect(isValidSkillName(name)).toBe(true);
  });

  it("ignores a directory whose name is hostile, without reading it", () => {
    const root = bundleWith([{ name: "good" }]);
    const traversal = join(root, "skills", "..");
    // `..` resolves to the bundle root; discoverSkills must skip it by NAME.
    expect(existsSync(traversal)).toBe(true);
    const found = discoverSkills(root).map((s) => s.name);
    expect(found).toEqual(["good"]);
  });

  it("takes the FIRST name, so a duplicate key cannot override the validated one", () => {
    expect(parseSkillFrontmatter("---\nname: good\nname: safe),Bash\ndescription: d\n---\n"))
      .toEqual({ name: "good", description: "d" });
  });

  it("requires the frontmatter name to match its directory", () => {
    const root = bundleWith([]);
    const dir = join(root, "skills", "claims-one-thing");
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, "SKILL.md"), "---\nname: something-else\ndescription: d\n---\n");
    expect(discoverSkills(root)).toEqual([]);
  });

  it("bounds the discovery read rather than slurping a huge file", () => {
    // The cap protects THIS pass, not the SDK (which reads the file itself on
    // invocation). Frontmatter lives at the top, so a long body is still a
    // legitimate skill — the property that matters is that discovery never
    // reads more than the cap.
    const root = bundleWith([{ name: "good" }]);
    const big = join(root, "skills", "huge");
    mkdirSync(big, { recursive: true });
    writeFileSync(join(big, "SKILL.md"),
      "---\nname: huge\ndescription: d\n---\n" + "x".repeat(MAX_SKILL_FILE_BYTES + 1024));
    expect(discoverSkills(root).map((s) => s.name)).toEqual(["good", "huge"]);
  });

  it("skips a file whose frontmatter lies BEYOND the cap", () => {
    // Truncation loses the closing `---`, so the frontmatter cannot parse and
    // the skill is dropped instead of being half-read.
    const root = bundleWith([{ name: "good" }]);
    const pushed = join(root, "skills", "pushed");
    mkdirSync(pushed, { recursive: true });
    writeFileSync(join(pushed, "SKILL.md"),
      "---\nname: pushed\ndescription: " + "y".repeat(MAX_SKILL_FILE_BYTES + 64) + "\n---\n");
    expect(discoverSkills(root).map((s) => s.name)).toEqual(["good"]);
  });
});

describe("tier binding — the fail-closed boundary", () => {
  it("refuses to mount an operator bundle in a tenant-tier process", () => {
    const operator = bundleWith([{ name: "operator-only-skill" }], "operator");
    const att = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: operator, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv);
    expect(att).toBeNull();
  });

  it("refuses a bundle that declares no tier at all", () => {
    const untagged = bundleWith([{ name: "probe" }], null);
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: untagged, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("refuses an unknown process tier", () => {
    const root = bundleWith([{ name: "probe" }]);
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "superuser",
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("mounts when the tiers agree, echoing the tier", () => {
    const root = bundleWith([{ name: "probe" }], "operator");
    const att = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: root, LEAF_SKILLS_TIER: "operator",
    } as NodeJS.ProcessEnv);
    expect(att?.tier).toBe("operator");
    expect(att?.skills).toEqual(["probe"]);
  });

  it("refuses a bundle path that is a FILE, not a directory", () => {
    const root = bundleWith([{ name: "probe" }]);
    const file = join(root, ".claude-plugin", "plugin.json");
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: file, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });
});

describe("symlink containment — the escape the review found", () => {
  // A tenant-safe wrapper bundle whose skills/<x> is a SYMLINK to an operator
  // skill outside the bundle. statSync and readFileSync both follow links, so
  // without a real-path containment check the external skill lands in the
  // allowlist and the wrapper still mounts as "tenant-safe".
  function trySymlink(target: string, path: string, type: "dir" | "file"): boolean {
    try { symlinkSync(target, path, type); return true; } catch { return false; }
  }

  it("does not follow a symlinked skill directory out of the bundle", () => {
    const outside = bundleWith([{ name: "operator-only-skill" }], "operator");
    const wrapper = bundleWith([{ name: "innocent" }], "tenant-safe");
    const linked = join(wrapper, "skills", "operator-only-skill");
    if (!trySymlink(join(outside, "skills", "operator-only-skill"), linked, "dir")) {
      return; // unprivileged Windows cannot create symlinks; nothing to assert
    }
    const names = discoverSkills(wrapper).map((s) => s.name);
    expect(names).toEqual(["innocent"]);
    const att = skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: wrapper, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv);
    expect(att?.skills ?? []).not.toContain("operator-only-skill");
  });

  it("does not follow a symlinked SKILL.md out of the bundle", () => {
    const outside = bundleWith([{ name: "operator-only-skill" }], "operator");
    const wrapper = bundleWith([{ name: "innocent" }], "tenant-safe");
    const dir = join(wrapper, "skills", "operator-only-skill");
    mkdirSync(dir, { recursive: true });
    if (!trySymlink(join(outside, "skills", "operator-only-skill", "SKILL.md"),
                    join(dir, "SKILL.md"), "file")) {
      return;
    }
    expect(discoverSkills(wrapper).map((s) => s.name)).toEqual(["innocent"]);
  });

  it("refuses a bundle whose MANIFEST is a symlink to another tier's manifest", () => {
    // Otherwise a wrapper could borrow a tenant-safe manifest while serving
    // operator skills.
    const tenant = bundleWith([{ name: "innocent" }], "tenant-safe");
    const wrapper = bundleWith([{ name: "operator-only-skill" }], null);
    const manifestPath = join(wrapper, ".claude-plugin", "plugin.json");
    rmSync(manifestPath, { force: true });
    if (!trySymlink(join(tenant, ".claude-plugin", "plugin.json"), manifestPath, "file")) {
      return;
    }
    expect(readBundleTier(wrapper)).toBeNull();
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: wrapper, LEAF_SKILLS_TIER: "tenant-safe",
    } as NodeJS.ProcessEnv)).toBeNull();
  });
});

describe("converse runner wiring", () => {
  // The failure this catches: the helper is perfect and nothing calls it. A
  // declaration can also be swallowed by a comment block and still compile —
  // that exact bug shipped in App.jsx earlier in this program. esbuild strips
  // comments, so surviving the transform is evidence the wiring is live code.
  it("passes the bundle's plugin and skills into the SDK query", async () => {
    const esbuild = await import("esbuild");
    const { readFileSync } = await import("node:fs");
    const source = readFileSync(
      new URL("../src/ports/impl/agentSdkTurnRunner.ts", import.meta.url), "utf8");
    const stripped = esbuild.transformSync(source, { loader: "ts" }).code;

    expect(stripped).toMatch(/skillBundleAttachment\s*\(/);
    // The options spread must carry BOTH: plugins alone would mount the bundle
    // without enabling anything, skills alone would name skills with no source.
    expect(stripped).toMatch(/plugins:\s*\[\s*\w+\.plugin\s*\]/);
    expect(stripped).toMatch(/skills:\s*\w+\.skills/);
    // settingSources must stay empty — it is what keeps ~/.claude out, and it
    // cannot name a curated directory anyway.
    expect(stripped).toMatch(/settingSources:\s*\[\s*\]/);
  });
});
