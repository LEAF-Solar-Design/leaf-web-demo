// The ios_surface build fence, in the ONE static form Vite can fold.
//
// `import.meta.env.VITE_IOS_SURFACE === '1'` is written literally here and
// nowhere else: Vite replaces the `import.meta.env.X` member expression with a
// string literal at build time. Callers pass this to <IosSurface enabled=...>:
// when the flag is off the component renders its DORMANT placeholder (the
// envelope's "iOS tab shows the dormant placeholder" negative control), and
// the status fetch is skipped, so no /api/ios-surface/status call is made
// against the backend that would 404-refuse anyway.
//
// Note: unlike ENV_LIFECYCLE_UI, this surface is deliberately RENDERED (dormant)
// when off rather than stripped, because the envelope requires a visible dormant
// placeholder. The real security boundary is server-side (the route refuses 404
// while the LEAF_IOS_SURFACE_ENABLED flag is off); the consume-only placeholder
// reveals no readiness detail.
//
// Deliberately NOT `import.meta.env?.` — the optional chain defeats Vite's
// static replacement.
export const ENV_IOS_SURFACE = import.meta.env.VITE_IOS_SURFACE === '1'
