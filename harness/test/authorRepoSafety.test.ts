import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const source = readFileSync(
  new URL("../src/agent/authorLoop.ts", import.meta.url),
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
});
