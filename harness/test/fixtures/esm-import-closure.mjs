/**
 * Walk the REAL ESM import closure of a module and report what it reaches.
 *
 * Run as a subprocess by dependencyExposure.test.ts because `import.meta.resolve`
 * is unavailable under vite's SSR transform, and hand-resolution got this wrong
 * twice: once by ignoring package subpaths, once by using CommonJS resolution
 * and walking dist/cjs/** while this ESM project loads dist/esm/**. Using node's
 * own ESM resolver is the only version that walks what actually gets loaded.
 *
 * Usage: node esm-import-closure.mjs <entry-specifier> <pattern>
 * Prints JSON: { files, unresolved, offenders }
 */
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";

const [entry, pattern] = process.argv.slice(2);
const needle = new RegExp(pattern, "i");

const seen = new Set();
const unresolved = [];
const offenders = [];

function resolveFrom(specifier, parentPath) {
  return fileURLToPath(import.meta.resolve(specifier, pathToFileURL(parentPath).href));
}

// SELF-CHECK, first thing: node's STABLE import.meta.resolve ignores the second
// argument, resolving against this script instead of the parent file. It does
// not throw — it returns a confidently wrong path that simply does not exist,
// so the walk stopped at 2 files with zero unresolved edges and looked healthy.
// The parent form needs --experimental-import-meta-resolve. If the flag is
// missing, FAIL LOUDLY rather than report a tiny clean closure.
{
  const probe = resolveFrom("./package.json", process.cwd() + "/node_modules/@modelcontextprotocol/sdk/dist/esm/server/mcp.js");
  if (!probe.includes("modelcontextprotocol")) {
    process.stdout.write(JSON.stringify({
      files: 0,
      unresolved: ["RESOLVER IGNORED THE PARENT — run with --experimental-import-meta-resolve"],
      offenders: [],
    }));
    process.exit(0);
  }
}

function walk(file) {
  if (seen.has(file) || !existsSync(file)) return;
  seen.add(file);

  const raw = readFileSync(file, "utf8");
  if (needle.test(raw)) offenders.push(file);

  // Comments stripped first: JSDoc `@param {import('./types')}` annotations
  // look exactly like dynamic imports and point at .d.ts files node never
  // loads, so counting them would mean whitelisting real-looking failures.
  const text = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*/g, "$1");

  const specifiers = new Set([
    ...[...text.matchAll(/from\s*["']([^"']+)["']/g)].map((m) => m[1]),
    ...[...text.matchAll(/import\s*["']([^"']+)["']/g)].map((m) => m[1]),
    ...[...text.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1]),
    ...[...text.matchAll(/require\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1]),
  ]);

  for (const specifier of specifiers) {
    if (specifier.startsWith("node:")) continue;
    try {
      walk(resolveFrom(specifier, file));
    } catch {
      // ESM resolution requires explicit extensions; CommonJS does not. ajv's
      // dist/*.js are CJS and require("./compile/codegen") extensionless, which
      // is a REAL runtime edge node resolves via CJS rules. Falling back here
      // is not the earlier mistake of using CJS for everything — ESM is tried
      // first, and this only covers files node itself loads as CommonJS.
      try {
        walk(createRequire(file).resolve(specifier));
      } catch {
        unresolved.push(`${specifier} (from ${file})`);
      }
    }
  }
}

walk(resolveFrom(entry, process.cwd() + "/package.json"));
process.stdout.write(JSON.stringify({ files: seen.size, unresolved, offenders }));
