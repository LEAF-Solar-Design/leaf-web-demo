// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
import { resolve } from "node:path";

export * from "../../vendor/mushy-author/ports/impl/sessionStore.js";

/**
 * The directory FileSessionStore resolves when constructed without `dir`:
 * LEAF_SESSIONS_DIR, else ./sessions-data, made absolute. Pure: reads env only.
 * The readiness probe uses it so the probe and the store name the same directory;
 * startReal passes it to the store explicitly.
 */
export function resolveSessionsDir(env: NodeJS.ProcessEnv = process.env): string {
  return resolve(env.LEAF_SESSIONS_DIR ?? "sessions-data");
}
