/**
 * Card D-4: credential-material grep assertion over the real D-slice files.
 *
 * Attempt 2 (this file): the readiness -> stages -> receipt e2e and the
 * ios_surface OFF flip-time fence now drive the REAL Python route
 * (server/routers/ios_surface.py) directly, via FastAPI TestClient, in
 * server/tests/test_ios_surface_e2e_harness.py -- not reimplemented here.
 * The rendered-UI half lives in web/src/ios/iosSurfaceServerContract.test.jsx,
 * which renders the REAL, D-2-merged IosSurface.jsx fed by the contract JSON
 * that Python test captured straight from the real server route + real
 * validate_contract.
 *
 * What stays here is genuinely harness-only: a static grep over the real
 * D-slice source files for secret-shaped literal VALUES (PEM private-key
 * blocks, JWT-looking blobs), with a seeded-violation fixture proving the
 * grep has teeth (it must actually catch a planted signing-identity-shaped
 * literal in a temp file dropped into the scanned tree, then clean up).
 */
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

// Value-pattern only, deliberately: a key-name pattern would also match this
// router's own SECRET_KEY_RE literal text and legitimate field names like
// "credential" in comments/tests. Matching on literal secret VALUES -- PEM
// private-key blocks, JWT-looking blobs -- distinguishes an actual leaked
// credential from source that merely talks about credentials, mirroring
// server/tests/test_ios_surface_contract.py::test_source_file_carries_no_hardcoded_secret_material.
const FORBIDDEN_VALUE_PATTERNS: RegExp[] = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
  /eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}/,
];

/** Every *.py/*.js/*.jsx file directly under `dir` (non-recursive on purpose:
 * the D-slice targets below are flat leaf directories, not trees). */
function sourceFilesIn(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isFile() && /\.(py|js|jsx|ts|tsx)$/.test(entry.name))
    .map((entry) => join(dir, entry.name));
}

/** The real D-slice files this card's oracle names explicitly. */
function realDSliceFiles(): string[] {
  return [
    join(REPO_ROOT, "server", "routers", "ios_surface.py"),
    join(REPO_ROOT, "web", "src", "site", "iosShipReadiness.js"),
    ...sourceFilesIn(join(REPO_ROOT, "web", "src", "ios")), // D-2 surface + D-3 timeline projection
  ];
}

function grepForbiddenValues(text: string): RegExp[] {
  return FORBIDDEN_VALUE_PATTERNS.filter((pattern) => pattern.test(text));
}

describe("credential-material grep assertion (real D-slice files)", () => {
  it("no secret-shaped literal value appears anywhere in the real D-slice source files", () => {
    const files = realDSliceFiles();
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const text = readFileSync(file, "utf8");
      const hits = grepForbiddenValues(text);
      expect(hits, `forbidden secret-shaped literal in ${file}: ${hits.map((p) => p.source).join(", ")}`)
        .toHaveLength(0);
    }
  });

  describe("seeded-violation fixture: the grep genuinely has teeth", () => {
    let tempDir: string | null = null;

    afterEach(() => {
      if (tempDir) rmSync(tempDir, { recursive: true, force: true });
      tempDir = null;
    });

    it("catches a planted signing-identity-shaped private key literal", () => {
      tempDir = mkdtempSync(join(tmpdir(), "leaf-d4-grep-teeth-"));
      const plantedFile = join(tempDir, "planted_signing_identity.txt");
      const plantedSecret =
        "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7VJTUt9Us8cKj\n-----END PRIVATE KEY-----";
      writeFileSync(plantedFile, plantedSecret, "utf8");

      const hits = grepForbiddenValues(readFileSync(plantedFile, "utf8"));
      expect(hits.length, "the planted signing-identity literal must be caught").toBeGreaterThan(0);

      // Prove the real D-slice files (unmodified) do NOT trip the same check
      // this seeded fixture just proved catches a real violation.
      for (const file of realDSliceFiles()) {
        expect(grepForbiddenValues(readFileSync(file, "utf8"))).toHaveLength(0);
      }
    });
  });
});
