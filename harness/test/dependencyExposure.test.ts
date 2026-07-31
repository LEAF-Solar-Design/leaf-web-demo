import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
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
    expect(source).not.toContain("serveStatic(");
    // NARROWED from the whole `@modelcontextprotocol/sdk/server` prefix to the
    // transport that actually carries the exposure. The Hono static-file server
    // is reached through server/streamableHttp; server/mcp.js is the in-process
    // McpServer class and does not touch it. The old prefix ban also forbade
    // that class, which blocked the guarded MCP proxy (mcpProxy.ts) — a change
    // whose whole purpose is to stop the SDK connecting to tenant URLs itself.
    // The next assertion is what keeps this narrowing honest.
    expect(source).not.toContain("@modelcontextprotocol/sdk/server/streamableHttp");
  });

  it("proves server/mcp.js cannot reach Hono, which is why importing it is allowed", () => {
    // The narrowing above is only safe while this holds, so it is CHECKED, not
    // asserted in a comment: walk the real transitive import closure of the
    // module we import and fail if any file in it mentions Hono.
    const root = join(process.cwd(), "node_modules/@modelcontextprotocol/sdk/dist/esm");
    const seen = new Set<string>();
    const offenders: string[] = [];
    const walk = (file: string): void => {
      if (seen.has(file) || !existsSync(file)) return;
      seen.add(file);
      const text = readFileSync(file, "utf8");
      if (/hono/i.test(text)) offenders.push(file);
      for (const match of text.matchAll(/from ["'](\.[^"']+)["']/g)) {
        const resolved = join(dirname(file), match[1]);
        walk(resolved.endsWith(".js") ? resolved : `${resolved}.js`);
      }
    };
    walk(join(root, "server/mcp.js"));

    expect(seen.size).toBeGreaterThan(1);
    expect(offenders).toEqual([]);
  });
});
