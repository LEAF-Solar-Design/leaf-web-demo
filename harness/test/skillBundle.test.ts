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
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";

import {
  discoverSkills,
  parseSkillFrontmatter,
  skillBundleAttachment,
} from "../src/ports/impl/skillBundle.js";

const made: string[] = [];

function bundleWith(skills: Array<{ name: string; description?: string; body?: string }>): string {
  const root = mkdtempSync(join(tmpdir(), "leaf-skills-"));
  made.push(root);
  mkdirSync(join(root, ".claude-plugin"), { recursive: true });
  writeFileSync(join(root, ".claude-plugin", "plugin.json"),
    JSON.stringify({ name: "leaf-test", version: "0.0.1" }));
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
  });

  it("is OFF when the configured path does not exist", () => {
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: join(tmpdir(), "leaf-absent-bundle"),
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("is OFF for an empty bundle — never enable the Skill tool with nothing behind it", () => {
    const root = bundleWith([]);
    expect(skillBundleAttachment({
      LEAF_SKILLS_BUNDLE_PATH: root,
    } as NodeJS.ProcessEnv)).toBeNull();
  });

  it("emits a local plugin with MCP discovery skipped (the harness owns MCP)", () => {
    const root = bundleWith([{ name: "probe" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root } as NodeJS.ProcessEnv);
    expect(att?.plugin).toEqual({ type: "local", path: root, skipMcpDiscovery: true });
  });

  it("ALWAYS emits an explicit allowlist, never 'all'", () => {
    // 'all' would also enable the CLI's bundled developer skills (run, review,
    // security-review) inside a tenant session. This is the guard.
    const root = bundleWith([{ name: "one" }, { name: "two" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root } as NodeJS.ProcessEnv);
    expect(Array.isArray(att?.skills)).toBe(true);
    expect(att?.skills).not.toBe("all");
    expect(att?.skills).toEqual(["one", "two"]);
  });

  it("derives the allowlist from disk, so it cannot drift from the bundle", () => {
    const root = bundleWith([{ name: "only-this-one" }]);
    const att = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: root } as NodeJS.ProcessEnv);
    expect(att?.skills).toEqual(["only-this-one"]);
  });

  it("mounts ONE bundle path — the tenant/operator split is a disk boundary", () => {
    // `skills` is a context filter, not a sandbox: unlisted skills stay on disk
    // and are readable. So two tiers must be two DIRECTORIES, and the
    // attachment must carry exactly the one it was pointed at.
    const tenant = bundleWith([{ name: "tenant-safe-skill" }]);
    const operator = bundleWith([{ name: "tenant-safe-skill" }, { name: "operator-only-skill" }]);
    const tenantAtt = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: tenant } as NodeJS.ProcessEnv);
    const operatorAtt = skillBundleAttachment({ LEAF_SKILLS_BUNDLE_PATH: operator } as NodeJS.ProcessEnv);
    expect(tenantAtt?.skills).toEqual(["tenant-safe-skill"]);
    expect(tenantAtt?.plugin.path).toBe(tenant);
    expect(operatorAtt?.skills).toContain("operator-only-skill");
    expect(operatorAtt?.plugin.path).toBe(operator);
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
