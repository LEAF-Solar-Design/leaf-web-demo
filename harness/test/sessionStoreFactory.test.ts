import { describe, expect, it } from "vitest";

import { PgSessionStore } from "../src/ports/impl/pgSessionStore.js";
import { FileSessionStore } from "../src/ports/impl/sessionStore.js";
import { createSessionStore } from "../src/ports/impl/sessionStoreFactory.js";

describe("createSessionStore", () => {
  it("keeps the file store as the default", async () => {
    const handle = createSessionStore({});
    expect(handle.kind).toBe("file");
    expect(handle.store).toBeInstanceOf(FileSessionStore);
    await handle.close();
  });

  it("rejects an unknown authority", () => {
    expect(() =>
      createSessionStore({ LEAF_HARNESS_SESSION_STORE: "redis" }),
    ).toThrow(/use file or postgres/);
  });

  it("fails closed when PostgreSQL is requested without a URL", () => {
    expect(() =>
      createSessionStore({ LEAF_HARNESS_SESSION_STORE: "postgres" }),
    ).toThrow(/requires LEAF_HARNESS_DATABASE_URL or DATABASE_URL/);
  });

  it("constructs PostgreSQL explicitly without creating schema", async () => {
    const handle = createSessionStore({
      LEAF_HARNESS_SESSION_STORE: "postgres",
      LEAF_HARNESS_DATABASE_URL: "postgresql://invalid.invalid/leaf",
    });
    expect(handle.kind).toBe("postgres");
    expect(handle.store).toBeInstanceOf(PgSessionStore);
    await handle.close();
  });
});
