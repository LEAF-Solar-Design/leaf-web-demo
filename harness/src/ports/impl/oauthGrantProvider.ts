// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
export * from "../../vendor/mushy-author/ports/impl/oauthGrantProvider.js";

/**
 * The directory FileTenantGrantStore resolves when constructed without `dir`:
 * LEAF_GRANTS_DIR, else C:/tmp/leaf-grants. Pure: reads env only. startReal
 * passes it to createTenantGrantStore so the readiness probe and the store name
 * the same directory.
 */
export function resolveGrantsDir(env: NodeJS.ProcessEnv = process.env): string {
  return env.LEAF_GRANTS_DIR ?? "C:/tmp/leaf-grants";
}
