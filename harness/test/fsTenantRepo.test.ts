/**
 * FsTenantRepo — the ONLY filesystem surface the author session gets. Pins the
 * two containment guarantees the contract leans on (HARNESS-CONTRACT §3):
 *
 *   1. any path that escapes the checkout root is rejected;
 *   2. the .git directory is off-limits in BOTH directions (sol-critic R5:
 *      a writable .git/config or .git/hooks would let model-authored content
 *      install filters/hooks that run during the register commit).
 *
 * Hermetic: temp dir only; no git, no network.
 */

import { mkdtempSync, rmSync } from "node:fs";
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

  it("dotfiles that merely RESEMBLE .git stay writable (.gitignore, .gitattributes)", () => {
    repo.writeFile(".gitignore", "__pycache__/\n");
    expect(repo.readFile(".gitignore")).toContain("__pycache__");
    repo.writeFile(".gitattributes", "*.py text\n");
    expect(repo.exists(".gitattributes")).toBe(true);
  });
});
