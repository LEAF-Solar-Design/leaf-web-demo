import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("../src/agent/authorLoop.ts", import.meta.url),
  "utf8",
);
const changeRepoSource = readFileSync(
  new URL("../src/ports/impl/tenantChangeRepo.ts", import.meta.url),
  "utf8",
);

describe("author repository validation safety", () => {
  it("binds Git trust to the exact resolved checkout without wildcard trust", () => {
    expect(source).toContain("const resolvedRepoDir = realpathSync(repoDir)");
    expect(source).toContain('`safe.directory=${resolvedRepoDir}`');
    expect(source).toContain('"-C",\n        resolvedRepoDir');
    expect(source).not.toContain('safe.directory=*');
  });

  it("scrubs credentials from the validation Git child", () => {
    expect(source).toMatch(
      /--untracked-files=all[\s\S]{0,160}env: scrubSecrets\(process\.env\)/,
    );
  });

  it("binds every change-repo Git child to its exact resolved cwd", () => {
    expect(changeRepoSource).toContain("const resolvedCwd = realpathSync(cwd)");
    expect(changeRepoSource).toContain('const safeDirectory = resolvedCwd.replaceAll("\\\\", "/")');
    expect(changeRepoSource).toContain('["-c", `safe.directory=${safeDirectory}`, ...args]');
    expect(changeRepoSource).toContain("cwd: resolvedCwd");
    expect(changeRepoSource).toContain("env: scrubSecrets(process.env)");
    expect(changeRepoSource).not.toContain("safe.directory=*");
    expect(changeRepoSource).not.toContain('"config", "--global"');
  });
});
