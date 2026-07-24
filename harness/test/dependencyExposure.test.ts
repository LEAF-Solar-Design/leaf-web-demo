import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

function sourceFiles(root: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory()) files.push(...sourceFiles(path));
    else if (entry.isFile() && path.endsWith(".ts")) files.push(path);
  }
  return files;
}

describe("transitive HTTP dependency exposure", () => {
  it("does not mount the vulnerable transitive Hono static-file server", () => {
    const source = sourceFiles(join(process.cwd(), "src"))
      .map((path) => readFileSync(path, "utf8"))
      .join("\n");

    expect(source).not.toContain("@hono/node-server");
    expect(source).not.toContain("@modelcontextprotocol/sdk/server");
    expect(source).not.toContain("serveStatic(");
  });
});
