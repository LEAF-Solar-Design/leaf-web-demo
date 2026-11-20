import { readFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { AgentGrant } from "../src/ports/index.js";
import { OpenAiConverseRunner } from "../src/ports/impl/openaiConverseRunner.js";
import { createTenantRunnerFor } from "../src/ports/impl/tenantRunnerFor.js";

const grant: AgentGrant = { kind: "oauth", oauthToken: "g" };
const record = {
  provider: "openai_compatible", base_url: "https://llm.example.test/v1", model: "qwen3.8-27b",
  api_key_env: "LEAF_PROVIDER_KEY_MINI", label: "local on Mac mini",
};
const env = { LEAF_TENANT_PROVIDER_HOSTS: "llm.example.test", LEAF_PROVIDER_KEY_MINI: "mini-key-not-real-000000000000" };

let dir: string;
beforeEach(async () => { dir = await mkdtemp(join(tmpdir(), "tenant-runner-for-")); });
afterEach(async () => { await rm(dir, { recursive: true, force: true }); });

describe("createTenantRunnerFor (shared by serve.ts and server.ts)", () => {
  it("picks the OpenAI-compatible runner for a tenant with a record and nothing (Claude) otherwise", async () => {
    await writeFile(join(dir, "film-tenant.provider.json"), JSON.stringify(record));
    const tenantRunnerFor = createTenantRunnerFor({ grantsDir: dir, env });
    const picked = await tenantRunnerFor("film-tenant", grant);
    expect(picked?.runner).toBeInstanceOf(OpenAiConverseRunner);
    expect((picked?.runner as unknown as { options: Record<string, unknown> }).options).toMatchObject({
      model: "qwen3.8-27b", baseUrl: "https://llm.example.test/v1",
      identity: { provider: "openai_compatible", label: "local on Mac mini" },
    });
    expect(picked?.secrets).toEqual([env.LEAF_PROVIDER_KEY_MINI]);
    expect(await tenantRunnerFor("other-tenant", grant)).toBeNull();
  });

  it("fails closed when a record exists but cannot be honoured", async () => {
    await writeFile(join(dir, "film-tenant.provider.json"), JSON.stringify(record));
    await expect(createTenantRunnerFor({ grantsDir: dir, env: { ...env, LEAF_PROVIDER_KEY_MINI: "" } })(
      "film-tenant", grant)).rejects.toThrow("provider key unavailable");
    await expect(createTenantRunnerFor({ grantsDir: dir, env: { LEAF_PROVIDER_KEY_MINI: "x" } })(
      "film-tenant", grant)).rejects.toThrow("base_url host not allowed");
  });
});

describe("tenant provider entrypoint wiring", () => {
  for (const entrypoint of ["../scripts/serve.ts", "../src/server.ts"]) {
    it(`${entrypoint} hands SpineTurnAdapter the shared tenant runner factory over the grant dir`, () => {
      const source = readFileSync(new URL(entrypoint, import.meta.url), "utf8");
      expect(source).toMatch(/tenantRunnerFor:\s*createTenantRunnerFor\(\{\s*grantsDir\b/);
      expect(source).not.toMatch(/resolveTenantProvider\s*\(/);
    });
  }
});
