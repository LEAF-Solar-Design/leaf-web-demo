import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
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

  it("proves importing server/mcp.js does not load Hono, which is why it is allowed", () => {
    // OBSERVED, not simulated. Five hand-written closure walkers were wrong
    // here before this, every one of them SILENTLY:
    //   1. ignored package subpaths — the walk stopped dead;
    //   2. createRequire().resolve() is CJS resolution, so it walked dist/cjs/**
    //      while this ESM project loads dist/esm/**;
    //   3. node's stable import.meta.resolve ignores its parent argument and
    //      returns a confident wrong path — the walk collapsed to 2 files while
    //      reporting zero unresolved edges;
    //   4. it does not throw on extensionless CJS specifiers, so a nonexistent
    //      target was skipped rather than reported;
    //   5. the ENTRY was still resolved with CJS conditions. Both closures held
    //      222 files while sharing only 71 — matching a COUNT is not matching a
    //      SET, and that coincidence hid the defect for a whole round.
    //
    // Simulation kept losing because it re-implements a resolver with more edge
    // cases than anyone can hold in their head. Node already has that resolver,
    // so this imports the module under loader hooks and records what node
    // actually evaluates. There is nothing left to get subtly wrong.
    const out = join(tmpdir(), `leaf-loaded-${process.pid}.json`);
    execFileSync(process.execPath,
      [join(process.cwd(), "test/fixtures/record-loaded-modules.mjs"),
       "@modelcontextprotocol/sdk/server/mcp.js", out],
      { cwd: process.cwd(), encoding: "utf8", timeout: 120_000 });
    const loaded = JSON.parse(readFileSync(out, "utf8")) as string[];
    rmSync(out, { force: true });

    // Sanity: the recording actually happened, and it recorded the ESM tree
    // this project loads rather than the CJS twin.
    expect(loaded.length).toBeGreaterThan(50);
    expect(loaded.some((file) => /[\\/]dist[\\/]esm[\\/]server[\\/]mcp\.js$/.test(file))).toBe(true);
    expect(loaded.filter((file) => /[\\/]dist[\\/]cjs[\\/]/.test(file))).toEqual([]);

    // THE assertion: nothing node loads for this import is Hono.
    expect(loaded.filter((file) => /hono/i.test(file))).toEqual([]);
    // Vitest's DEFAULT 5s test timeout is not enough for this one. It spawns a
    // node subprocess that loads the whole SDK under loader hooks: ~1.4s alone,
    // but well past 5s when the full suite is running in parallel around it.
    // Caught by the full gate after it passed standalone — the exact shape of a
    // test that is green locally and flaky in CI. The budget matches the
    // subprocess's own 120s ceiling so one bound governs, not two.
  }, 120_000);
});
