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
    // The narrowing above is only safe while this holds, so it is CHECKED.
    // Round 1 was right that the first version of this walker was too weak: it
    // followed only relative `from` imports, so a dynamic import, a
    // side-effect import, or a hop into another PACKAGE could have carried a
    // route it never saw. All three are followed now.
    const modules = join(process.cwd(), "node_modules");
    const seen = new Set<string>();
    const bare = new Set<string>();
    const offenders: string[] = [];

    const walk = (file: string): void => {
      if (seen.has(file) || !existsSync(file)) return;
      seen.add(file);
      const text = readFileSync(file, "utf8");
      if (/hono/i.test(text)) offenders.push(file);
      // `from "x"`, bare `import "x"`, and `import("x")` alike.
      const specifiers = [
        ...[...text.matchAll(/from\s*["']([^"']+)["']/g)].map((m) => m[1]),
        ...[...text.matchAll(/import\s*["']([^"']+)["']/g)].map((m) => m[1]),
        ...[...text.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1]),
      ];
      for (const specifier of specifiers) {
        if (specifier.startsWith(".")) {
          const resolved = join(dirname(file), specifier);
          walk(resolved.endsWith(".js") ? resolved : `${resolved}.js`);
        } else {
          // A hop out of the package. Record the package name; its own graph is
          // judged by dependency NAME below, which is what would reveal Hono
          // arriving through a future SDK update.
          bare.add(specifier.startsWith("@") ? specifier.split("/").slice(0, 2).join("/") : specifier.split("/")[0]);
        }
      }
    };
    walk(join(modules, "@modelcontextprotocol/sdk/dist/esm/server/mcp.js"));

    // Follow each bare hop INTO ITS FILES, not just its manifest. The
    // distinction is the whole finding: the MCP SDK package has always DEPENDED
    // on Hono (its server transports use it), so a manifest-level check fails
    // on a true-but-irrelevant fact. What the guard actually asks is whether
    // importing server/mcp.js ever LOADS Hono, and only the file graph answers
    // that. A first version of this assertion got it wrong and said so loudly,
    // which is the correct failure mode for a security guard.
    const entryOf = (name: string): string | null => {
      const manifest = join(modules, name, "package.json");
      if (!existsSync(manifest)) return null;
      const pkg = JSON.parse(readFileSync(manifest, "utf8"));
      const candidate = pkg.module
        ?? (typeof pkg.exports?.["."] === "string" ? pkg.exports["."] : pkg.exports?.["."]?.import?.default ?? pkg.exports?.["."]?.import)
        ?? pkg.main;
      return typeof candidate === "string" ? join(modules, name, candidate) : null;
    };
    for (const name of bare) {
      const entry = entryOf(name);
      if (entry) walk(entry);
    }

    expect(seen.size).toBeGreaterThan(1);
    expect(offenders).toEqual([]);
    // Bare hops were followed into their own files, so `seen` spans packages.
    expect(bare.size).toBeGreaterThan(0);
  });
});
