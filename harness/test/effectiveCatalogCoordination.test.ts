import { afterEach, expect, it, vi } from "vitest";
import { CustomizationCoordinationClient } from "../src/ports/impl/customizationCoordinationClient.js";

const commit = "a".repeat(40);
const digest = "b".repeat(64);
const client = () => new CustomizationCoordinationClient({ baseUrl: "https://app.test/", dispatchSecret: "fixture-secret" });
afterEach(() => vi.unstubAllGlobals());

it("requests a fresh tenant-bound catalog and forbids redirects", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ tenant_id: "tenant-a", catalog_commit: commit, catalog_digest: digest })));
  vi.stubGlobal("fetch", fetch);
  expect(await client().effectiveCatalog("tenant-a")).toEqual({ catalogCommit: commit, catalogDigest: digest });
  expect(fetch).toHaveBeenCalledWith("https://app.test/internal/customization/effective-catalog", expect.objectContaining({
    method: "GET", redirect: "error", headers: { "x-tenant-id": "tenant-a", "x-dispatch-secret": "fixture-secret", "cache-control": "no-store" },
  }));
});

it.each([
  { tenant_id: "other", catalog_commit: commit, catalog_digest: digest },
  { tenant_id: "tenant-a", catalog_commit: "main", catalog_digest: digest },
  { tenant_id: "tenant-a", catalog_commit: commit, catalog_digest: "bad" },
  null,
])("rejects a substituted or malformed catalog", async value => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(value))));
  await expect(client().effectiveCatalog("tenant-a")).rejects.toThrow("effective catalog coordination is unavailable");
});

it("does not expose failed response bodies or transport diagnostics", async () => {
  const fetch = vi.fn().mockResolvedValueOnce(new Response("fixture-sensitive-body", { status: 403 }))
    .mockRejectedValueOnce(new Error("fixture-sensitive-transport"));
  vi.stubGlobal("fetch", fetch);
  for (let i = 0; i < 2; i++) await expect(client().effectiveCatalog("tenant-a")).rejects.toThrow(/^effective catalog coordination is unavailable$/);
});

it("refuses missing dispatch configuration before network access", async () => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  await expect(new CustomizationCoordinationClient({ baseUrl: "https://app.test", dispatchSecret: "" }).effectiveCatalog("tenant-a")).rejects.toThrow("not configured");
  expect(fetch).not.toHaveBeenCalled();
});
