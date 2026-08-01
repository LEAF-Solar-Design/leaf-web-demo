/**
 * Loader hooks that RECORD every module node resolves, so the dependency guard
 * can observe what actually loads instead of simulating resolution.
 *
 * Five simulated walkers were wrong before this, every one silently:
 *   1. ignored package subpaths, so the walk stopped dead;
 *   2. createRequire().resolve() is CJS resolution — walked dist/cjs/** while
 *      this ESM project loads dist/esm/**;
 *   3. node's STABLE import.meta.resolve ignores its parent argument, returning
 *      a confident wrong path (walk collapsed to 2 files, 0 unresolved);
 *   4. import.meta.resolve does not throw on extensionless CJS specifiers, so a
 *      nonexistent target was skipped instead of reported;
 *   5. the ENTRY was still resolved with CJS conditions. Both closures happened
 *      to contain 222 files while sharing only 71 — matching a COUNT is not
 *      matching a SET, and the coincidence hid the defect through a round.
 *
 * Simulation kept failing because it re-implements a resolver with more edge
 * cases than anyone can hold. Node already has that resolver; this just writes
 * down what it does.
 */
import { createRequire, register } from "node:module";
import { writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const [target, outputPath] = process.argv.slice(2);
const recorded = new Set();

// The hooks run in a separate thread and report back over this port.
const { port1, port2 } = new MessageChannel();
port1.on("message", (url) => {
  if (typeof url === "string" && url.startsWith("file:")) recorded.add(fileURLToPath(url));
});
port1.unref();

register("./record-hooks.mjs", {
  parentURL: import.meta.url,
  data: { port: port2 },
  transferList: [port2],
});

// Importing is the whole point: whatever node loads gets recorded, including
// paths no hand-written walk would have predicted.
await import(target);

// Give the hook messages a tick to drain before writing.
await new Promise((resolve) => setTimeout(resolve, 250));

// THE LOADER HOOKS ARE NOT ENOUGH ON THEIR OWN, and the gap is not obvious:
// async `register()` hooks see ESM loads and the ENTRY of a CommonJS package
// reached from ESM, but NOT the plain `require()` calls made inside that
// package. So a CJS dependency's own subtree is invisible to `load`.
//
// That is not a theory. The first recording reported 153 modules with
// `ajv/dist/core.js` absent, even though ajv demonstrably loads at runtime —
// ajv's entry was recorded and everything it requires was not.
//
// `require.cache` IS the CJS module registry (Module._cache), shared process
// wide regardless of which module did the requiring, so draining it after the
// import captures exactly what the hooks cannot see. Union of the two is the
// full set of files node actually evaluated.
const cjsLoaded = Object.keys(createRequire(import.meta.url).cache);
for (const file of cjsLoaded) recorded.add(file);

writeFileSync(outputPath, JSON.stringify([...recorded]), "utf8");
