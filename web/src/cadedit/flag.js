// The cad_edit build fence, in the ONE static form Vite can fold.
//
// Mirrors web/src/projects/flag.js exactly, for the same reason:
// `import.meta.env.VITE_CAD_EDIT === '1'` is written literally here and
// nowhere else, so Vite's build-time string replacement folds this module to
// `false` when the flag is off. Callers must use it as the FIRST operand of a
// `&&` guard at the call site (`{ENV_CAD_EDIT && cond && <CadEditSurface/>}`)
// — that is what lets Rollup drop the JSX, then the unused import, then the
// whole editing surface (and the DXF engine it pulls in) out of the bundle. A
// runtime `if (!enabled) return null` INSIDE the component does not do that.
// web/src/cadedit/bundleFence.test.js is the oracle that proves it.
//
// Deliberately NOT `import.meta.env?.` — the optional chain defeats Vite's
// static replacement and silently turns this fence back into a runtime check.
export const ENV_CAD_EDIT = import.meta.env.VITE_CAD_EDIT === '1'
