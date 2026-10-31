import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const entrypoints = ["../scripts/serve.ts", "../src/server.ts"];

describe("grant store entrypoint wiring", () => {
  for (const entrypoint of entrypoints) {
    const source = readFileSync(new URL(entrypoint, import.meta.url), "utf8");

    it(`${entrypoint} supplies a key provider to every grant store`, () => {
      const calls = [...source.matchAll(/\bcreateTenantGrantStore\s*\(([^)]*)\)/g)];
      expect(calls.length).toBeGreaterThan(0);
      for (const call of calls) {
        expect(call[1]).toMatch(/^\s*\{[\s\S]*\}\s*$/);
        expect(call[1]).toMatch(/\bkeyProvider\s*(?::|[,}])/);
      }
    });

    it(`${entrypoint} builds the provider before resolving the backend`, () => {
      const provider = source.search(/\bgrantKeyProviderFromEnv\s*\(/);
      const backend = source.search(/\bresolveGrantStoreBackend\s*\(/);
      expect(provider).toBeGreaterThanOrEqual(0);
      expect(backend).toBeGreaterThan(provider);
    });

    it(`${entrypoint} logs the grant store backend at boot`, () => {
      expect(source).toContain("grant_store_backend=");
    });
  }

  it("serve imports the environment key provider", () => {
    const source = readFileSync(new URL("../scripts/serve.ts", import.meta.url), "utf8");
    expect(source).toMatch(
      /import\s*\{[^}]*\bgrantKeyProviderFromEnv\b[^}]*\}\s*from\s*["']\.\.\/src\/security\/grantEnvelope\/fromEnv\.js["']/,
    );
  });

  it("the container startup script runs serve", () => {
    // Dockerfile.harness copies harness/scripts/ to /app/scripts/ and uses this script as CMD.
    const source = readFileSync(new URL("../scripts/start-harness.sh", import.meta.url), "utf8");
    expect(source).toContain("exec node dist/scripts/serve.js");
  });
});
