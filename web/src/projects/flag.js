// The lifecycle_ui build fence, in the ONE static form Vite can fold.
//
// `import.meta.env.VITE_LIFECYCLE_UI === '1'` is written literally here and
// nowhere else: Vite replaces the `import.meta.env.X` member expression with a
// string literal at build time, so this whole module folds to `false` when the
// flag is off. Callers must use it as the FIRST operand of a `&&` guard at the
// call site (`{ENV_LIFECYCLE_UI && cond && <Panel/>}`) — that is what lets
// Rollup drop the JSX, then the unused import, then the entire component
// subtree out of the bundle. A runtime `if (!enabled) return null` INSIDE a
// component does not do that: the component (and every string in it) still
// ships. web/src/projects/bundleFence.test.js is the oracle that proves it.
//
// Deliberately NOT `import.meta.env?.` — the optional chain defeats Vite's
// static replacement and silently turns this fence back into a runtime check.
export const ENV_LIFECYCLE_UI = import.meta.env.VITE_LIFECYCLE_UI === '1'
