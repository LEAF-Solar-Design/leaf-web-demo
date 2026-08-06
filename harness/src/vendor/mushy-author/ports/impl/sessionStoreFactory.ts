import type { SessionStore } from "../index.js";
import { PgSessionStore } from "./pgSessionStore.js";
import { FileSessionStore } from "./sessionStore.js";

export interface SessionStoreHandle {
  kind: "file" | "postgres";
  store: SessionStore;
  close(): Promise<void>;
}

/**
 * Select the harness transcript authority at process composition time.
 *
 * The default stays file-backed until the PostgreSQL schema, backfill, parity,
 * and rollback gates have passed. An explicit PostgreSQL request fails closed
 * when its connection URL is absent. Schema creation is never implicit.
 */
export function createSessionStore(
  env: NodeJS.ProcessEnv = process.env,
): SessionStoreHandle {
  const kind = (env.LEAF_HARNESS_SESSION_STORE ?? "file").trim().toLowerCase();
  if (kind === "file") {
    return {
      kind,
      store: new FileSessionStore(),
      async close(): Promise<void> {},
    };
  }
  if (kind !== "postgres") {
    throw new Error(
      `unsupported LEAF_HARNESS_SESSION_STORE=${JSON.stringify(kind)}; use file or postgres`,
    );
  }

  const connectionString = (
    env.LEAF_HARNESS_DATABASE_URL ??
    env.DATABASE_URL ??
    ""
  ).trim();
  if (!connectionString) {
    throw new Error(
      "LEAF_HARNESS_SESSION_STORE=postgres requires LEAF_HARNESS_DATABASE_URL or DATABASE_URL",
    );
  }

  const store = new PgSessionStore({
    poolConfig: {
      connectionString,
      max: 5,
      application_name: "leaf-platform-harness",
    },
  });
  return {
    kind,
    store,
    close: () => store.close(),
  };
}
