/**
 * Walk the REAL import closure of a module and report what it reaches.
 *
 * Run as a subprocess by dependencyExposure.test.ts because `import.meta.resolve`
 * is unavailable under vite's SSR transform. This walk has been wrong FOUR
 * times, every one of them SILENTLY, which is why the design below is
 * defensive rather than clever:
 *
 *   1. ignored package subpaths — the walk stopped dead.
 *   2. createRequire().resolve() is COMMONJS resolution, so it walked
 *      dist/cjs/** while this ESM project loads dist/esm/**.
 *   3. node's STABLE import.meta.resolve ignores its parent argument, returning
 *      a confident wrong path; the walk collapsed to 2 files while reporting
 *      zero unresolved edges.
 *   4. import.meta.resolve does NOT throw on an extensionless CommonJS
 *      specifier: `require("./core")` resolved to `ajv/dist/core`, a path that
 *      does not exist, so an `existsSync` early-return skipped it and the CJS
 *      fallback never ran. 162 files against 222 actually loaded.
 *
 * The rule that kills that whole class: a resolved target that is not a real
 * file is an UNRESOLVED EDGE, never a silent skip. And each edge is resolved by
 * the PARENT'S MODULE FORMAT, because that is how node itself loads it.
 *
 * Usage: node --experimental-import-meta-resolve esm-import-closure.mjs <spec> <pattern>
 * Prints JSON: { files, unresolved, offenders }
 */
import { existsSync, readFileSync, statSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const [entry, pattern] = process.argv.slice(2);
const needle = new RegExp(pattern, "i");

const seen = new Set();
const unresolved = [];
const offenders = [];

const isFile = (path) => {
  try { return statSync(path).isFile(); } catch { return false; }
};

function resolveEsm(specifier, parentPath) {
  return fileURLToPath(import.meta.resolve(specifier, pathToFileURL(parentPath).href));
}

/** CommonJS or ESM, decided the way node decides it. */
function formatOf(file) {
  if (file.endsWith(".cjs")) return "commonjs";
  if (file.endsWith(".mjs")) return "module";
  let dir = dirname(file);
  for (let depth = 0; depth < 12; depth += 1) {
    const manifest = join(dir, "package.json");
    if (existsSync(manifest)) {
      try {
        return JSON.parse(readFileSync(manifest, "utf8")).type === "module" ? "module" : "commonjs";
      } catch { return "commonjs"; }
    }
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return "commonjs";
}

// SELF-CHECK, and it probes a RELATIVE specifier because that is the shape that
// failed. Probing "./package.json" passed happily while the resolver was still
// wrong for extensionless paths — a self-check has to exercise the failure mode
// it exists to catch.
{
  const anchor = join(process.cwd(), "node_modules/@modelcontextprotocol/sdk/dist/esm/server/mcp.js");
  let probe = "";
  try { probe = resolveEsm("./index.js", anchor); } catch { /* handled below */ }
  if (!isFile(probe) || !probe.includes("modelcontextprotocol")) {
    process.stdout.write(JSON.stringify({
      files: 0,
      unresolved: ["RESOLVER IGNORED THE PARENT — run with --experimental-import-meta-resolve"],
      offenders: [],
    }));
    process.exit(0);
  }
}

function walk(file) {
  if (seen.has(file)) return;
  seen.add(file);

  const raw = readFileSync(file, "utf8");
  if (needle.test(raw)) offenders.push(file);

  // Comments stripped first: JSDoc `@param {import('./types')}` annotations
  // look exactly like dynamic imports and point at .d.ts files node never
  // loads, so counting them would mean whitelisting real-looking failures.
  const text = raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:])\/\/.*/g, "$1");

  const edges = [
    ...[...text.matchAll(/from\s*["']([^"']+)["']/g)].map((m) => [m[1], "esm"]),
    ...[...text.matchAll(/import\s*["']([^"']+)["']/g)].map((m) => [m[1], "esm"]),
    ...[...text.matchAll(/import\(\s*["']([^"']+)["']\s*\)/g)].map((m) => [m[1], "esm"]),
    ...[...text.matchAll(/require\(\s*["']([^"']+)["']\s*\)/g)].map((m) => [m[1], "cjs"]),
  ];

  const parentFormat = formatOf(file);
  const tried = new Set();

  for (const [specifier, kind] of edges) {
    if (specifier.startsWith("node:") || tried.has(`${kind}:${specifier}`)) continue;
    tried.add(`${kind}:${specifier}`);

    // A require() edge, or any edge inside a CommonJS file, resolves the way
    // node resolves it THERE. This is not round 2's mistake of using CJS for
    // everything: the parent's format decides, per edge.
    const preferCjs = kind === "cjs" || parentFormat === "commonjs";
    const attempts = preferCjs
      ? [() => createRequire(file).resolve(specifier), () => resolveEsm(specifier, file)]
      : [() => resolveEsm(specifier, file), () => createRequire(file).resolve(specifier)];

    let target = "";
    for (const attempt of attempts) {
      let candidate = "";
      try { candidate = attempt(); } catch { continue; }
      // Resolved-but-not-a-file is the silent failure that hid 60 files. Only a
      // real file counts as resolved.
      if (isFile(candidate)) { target = candidate; break; }
    }

    if (!target) { unresolved.push(`${specifier} (from ${file})`); continue; }
    walk(target);
  }
}

walk(createRequire(join(process.cwd(), "package.json")).resolve(entry));
process.stdout.write(JSON.stringify({ files: seen.size, unresolved, offenders }));
