// STRANGLER SHIM (mushy-code extraction, 2026-08-06): this module moved to the
// vendored mushy-code library. The path and every export stay stable for all
// in-repo importers; the implementation lives at the re-exported location and
// is synced by scripts/sync-mushy-code.py (pin: harness/src/vendor/VENDOR-PIN.json).
import { Pool } from "pg";

import { createFileStoreProbe, createPgStoreProbe } from "../../storeReadiness.js";
import type { StoreProbe } from "../../storeReadiness.js";
import { createSessionStore } from "../../vendor/mushy-author/ports/impl/sessionStoreFactory.js";
import type { SessionStoreHandle } from "../../vendor/mushy-author/ports/impl/sessionStoreFactory.js";
import { PgSessionStore } from "./pgSessionStore.js";
import { FileSessionStore, resolveSessionsDir } from "./sessionStore.js";

export * from "../../vendor/mushy-author/ports/impl/sessionStoreFactory.js";

/** A session store handle that can also answer readiness for its own backend. */
export interface ProbedSessionStoreHandle extends SessionStoreHandle {
  /** Probes the store this handle built: the file store's directory, or one
   * client from the PostgreSQL store's own pool (never a second pool). */
  readiness: StoreProbe<"file" | "postgres">;
}

/**
 * createSessionStore with a readiness probe bound to the store it built. Same
 * selection, defaults and fail-closed errors as createSessionStore. For postgres
 * the handle owns the one pool: the store borrows it and the readiness probe
 * borrows one client from it, and close() ends it.
 */
export function createProbedSessionStore(
  env: NodeJS.ProcessEnv = process.env,
): ProbedSessionStoreHandle {
  const kind = (env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase();
  const connectionString = (
    env.LEAF_HARNESS_DATABASE_URL ??
    env.DATABASE_URL ??
    ""
  ).trim();
  if (kind === "file") {
    // FileSessionStore resolves its directory from process.env, as here.
    const dir = resolveSessionsDir();
    return {
      kind,
      store: new FileSessionStore({ dir }),
      async close(): Promise<void> {},
      readiness: { kind: "file", probe: createFileStoreProbe(dir) },
    };
  }
  if (kind !== "postgres" || !connectionString) {
    // An unsupported kind or a postgres request without a URL: the vendored
    // factory owns the fail-closed error, so its message stays the one contract.
    const unexpected = createSessionStore(env);
    void unexpected.close();
    throw new Error("session store selection failed closed");
  }

  const pool = new Pool({
    connectionString,
    max: 5,
    application_name: "leaf-platform-harness",
  });
  // One guarded probe per pool: a hanging acquisition is joined, never repeated.
  const probe = createPgStoreProbe(async () => {
    const client = await pool.connect();
    return {
      query: (sql: string) => client.query(sql),
      release: (destroy?: boolean) => client.release(destroy === true),
    };
  });
  const store = new PgSessionStore({ pool });
  return {
    kind: "postgres",
    store,
    close: () => pool.end(),
    readiness: { kind: "postgres", probe },
  };
}
