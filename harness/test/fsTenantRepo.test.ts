/**
 * FsTenantRepo — the ONLY filesystem surface the author session gets. Pins the
 * two containment guarantees the contract leans on (HARNESS-CONTRACT §3):
 *
 *   1. any path that escapes the checkout root is rejected;
 *   2. the .git directory is off-limits in BOTH directions (sol-critic R5:
 *      a writable .git/config or .git/hooks would let model-authored content
 *      install filters/hooks that run during the register commit);
 *   3. links committed in the checkout cannot bypass 1 or 2 (sol-critic
 *      round 3: `alias -> .git` would let `alias/config` reach `.git/config`
 *      past a lexical-only check) — any symlink/junction component is rejected.
 *
 * Hermetic: temp dir only; no git, no network. Link tests self-skip on
 * platforms that refuse link creation.
 */

import { mkdirSync, mkdtempSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FsTenantRepo } from "../src/agent/tools/fsTenantRepo.js";

describe("FsTenantRepo containment", () => {
  let root: string;
  let repo: FsTenantRepo;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), "leaf-fsrepo-"));
    repo = new FsTenantRepo(root);
  });
  afterEach(() => {
    rmSync(root, { recursive: true, force: true });
  });

  it("normal read/write inside the checkout works", () => {
    repo.writeFile("tools/demo/tool.py", "def run(intake, params):\n    return ({}, None)\n");
    expect(repo.readFile("tools/demo/tool.py")).toContain("def run");
    expect(repo.exists("tools/demo/tool.py")).toBe(true);
    expect(repo.listDir("tools")).toContain("demo");
  });

  it("rejects absolute paths and parent-dir escapes", () => {
    expect(() => repo.writeFile(join(root, "..", "outside.txt"), "x")).toThrow(/absolute|escapes/);
    expect(() => repo.writeFile("../outside.txt", "x")).toThrow(/escapes/);
    expect(() => repo.readFile("../../etc/passwd")).toThrow(/escapes/);
  });

  it(".git is off-limits for writes, reads, listing — any depth, any case", () => {
    expect(() => repo.writeFile(".git/config", "[core]")).toThrow(/off-limits/);
    expect(() => repo.writeFile(".git/hooks/pre-commit", "#!/bin/sh")).toThrow(/off-limits/);
    expect(() => repo.writeFile("sub/.git/config", "[core]")).toThrow(/off-limits/);
    expect(() => repo.writeFile(".GIT/config", "[core]")).toThrow(/off-limits/);
    expect(() => repo.readFile(".git/config")).toThrow(/off-limits/);
    expect(() => repo.listDir(".git")).toThrow(/off-limits/);
    expect(() => repo.exists(".git/config")).toThrow(/off-limits/);
  });

  /** Junction on Windows (no privilege needed), dir symlink elsewhere. */
  function makeDirLink(target: string, linkPath: string): boolean {
    try {
      symlinkSync(target, linkPath, process.platform === "win32" ? "junction" : "dir");
      return true;
    } catch {
      return false;
    }
  }

  it("a link inside the checkout cannot smuggle access into .git (round-3 finding)", (ctx) => {
    mkdirSync(join(root, ".git"));
    if (!makeDirLink(join(root, ".git"), join(root, "alias"))) ctx.skip();
    expect(() => repo.writeFile("alias/config", "[core]")).toThrow(/off-limits/);
    expect(() => repo.readFile("alias/config")).toThrow(/off-limits/);
    expect(() => repo.listDir("alias")).toThrow(/off-limits/);
  });

  it("a link targeting OUTSIDE the checkout is rejected", (ctx) => {
    const outside = mkdtempSync(join(tmpdir(), "leaf-fsrepo-out-"));
    try {
      if (!makeDirLink(outside, join(root, "exit"))) ctx.skip();
      expect(() => repo.writeFile("exit/pwned.txt", "x")).toThrow(/off-limits/);
      expect(() => repo.exists("exit/pwned.txt")).toThrow(/off-limits/);
    } finally {
      rmSync(outside, { recursive: true, force: true });
    }
  });

  it("dotfiles that merely RESEMBLE .git stay writable (.gitignore, .gitattributes)", () => {
    repo.writeFile(".gitignore", "__pycache__/\n");
    expect(repo.readFile(".gitignore")).toContain("__pycache__");
    repo.writeFile(".gitattributes", "*.py text\n");
    expect(repo.exists(".gitattributes")).toBe(true);
  });
});
