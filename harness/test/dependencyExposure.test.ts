import { execFileSync } from "node:child_process";
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
    // The narrowing above is only safe while this holds, so it is CHECKED — and
    // the check was wrong TWICE before it was right, which is worth recording
    // because a security guard that quietly under-walks is worse than none:
    //   r1: followed only relative `from` imports.
    //   r2: discarded package SUBPATHS, so @modelcontextprotocol/sdk/validation/ajv
    //       resolved to a nonexistent index and the walk stopped silently.
    //   then: createRequire().resolve() is COMMONJS resolution, so it walked
    //       dist/cjs/** while this ESM project loads dist/esm/** — the guard was
    //       proving something about a copy that never runs. Caught by injecting
    //       Hono into the ESM twin and watching the test stay green.
    // The walk therefore runs in a real node subprocess using node's own ESM
    // resolver, which is the only resolver that sees what actually loads.
    const script = join(process.cwd(), "test/fixtures/esm-import-closure.mjs");
    const raw = execFileSync(process.execPath,
      // The flag is REQUIRED: without it import.meta.resolve ignores the parent
      // and returns wrong-but-existing-looking paths, which made the walk stop
      // at 2 files while reporting zero unresolved edges. The script self-checks
      // for this and reports it as an unresolved edge rather than a clean pass.
      ["--experimental-import-meta-resolve", script, "@modelcontextprotocol/sdk/server/mcp.js", "hono"],
      { cwd: process.cwd(), encoding: "utf8", timeout: 120_000 });
    const closure = JSON.parse(raw) as { files: number; unresolved: string[]; offenders: string[] };

    // A walk that cannot resolve an edge has not proved anything about it.
    expect(closure.unresolved).toEqual([]);
    // Guards against the walk silently collapsing to a handful of files again.
    expect(closure.files).toBeGreaterThan(200);
    expect(closure.offenders).toEqual([]);
  });
});
