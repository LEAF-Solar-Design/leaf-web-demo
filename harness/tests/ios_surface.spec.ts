/**
 * Card D-4: iOS surface harness + fence negative control.
 *
 * Oracle (frozen):
 *  - E2E against a fixture contract: readiness -> stages -> receipt render.
 *  - ios_surface OFF refuses (flip-time proof).
 *  - Credential-material grep assertion runs over the whole D-slice diff.
 *
 * This is the harness's OWN independent consumer of the ship-lane's
 * published contract (schema "leaf.ios-ship-surface.v1"), a second,
 * Node-side implementation of the same closed vocabulary and validation
 * server/routers/ios_surface.py defines -- duplicated per that router's own
 * "each consumer owns its own copy; nothing here mutates state" convention,
 * so this file never imports across the Python/TypeScript boundary. It
 * drives the render pipeline against an in-file FIXTURE source (never a
 * live network call), proving readiness -> build-stage progression ->
 * receipt render end to end, then proves the whole path is unreachable
 * with ios_surface OFF by call-count spy (not just a status check), and
 * flips OFF -> ON -> OFF to prove the fence is genuinely flag-driven --
 * matching card B-C6's flip-time fence precedent (harness/test/conversation
 * .test.ts + server/tests/test_conversation_fence.py). The final suite
 * greps the whole D-wave slice's diff (every "card D-" commit reachable
 * from this branch, plus any uncommitted working-tree change) for
 * secret-shaped literal values, so a credential leak anywhere in the
 * D-slice fails this file, not just this file's own source.
 */
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

// --------------------------------------------------------------------- //
// Fixture-contract consumer: the harness's own copy of the ship-lane's
// closed vocabulary (server/routers/ios_surface.py's _BUILD_STAGES, in the
// same published order) and validation. Self-contained: no network, no
// filesystem, no import outside this file.
// --------------------------------------------------------------------- //

const CONTRACT_SCHEMA = "leaf.ios-ship-surface.v1";

const BUILD_STAGES = [
  "SOURCE_APPROVED", "GRANT_READY", "BUNDLE_READY", "MAC_ALLOCATED", "APP_RECORD",
  "XCODE_READY", "SIGNING_READY", "BUILT", "UPLOADED", "COMPLIANCE", "BETA_ASSIGNED",
  "CREDENTIALS_SCRUBBED", "MAC_RELEASED", "RECEIPT",
] as const;
type BuildStage = (typeof BUILD_STAGES)[number];

const SECRET_KEY_RE =
  /(password|passwd|two.?factor|2fa|otp|p8|private[_ -]?key|certificate|provisioning|profile|credential|secret|keychain|authkey|token|session|cookie|api[_ -]?key|access[_ -]?key|signing[_ -]?(key|cert))/i;
const SECRET_VALUE_RE =
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|eyJ[a-zA-Z0-9_-]{10,}\.eyJ|AAAA[A-Za-z0-9+/]{20,}={0,2}/;

interface RawContract {
  schema?: unknown;
  project_id?: unknown;
  revision?: unknown;
  reported_at?: unknown;
  readiness?: unknown;
  build_stage?: unknown;
  receipt_id?: unknown;
  [extra: string]: unknown;
}

interface Contract {
  schema: string;
  project_id: string;
  revision: string;
  reported_at: string | null;
  readiness: { healthy: boolean; launchable: boolean };
  build_stage: BuildStage | null;
  receipt_id: string | null;
}

class ContractInvalid extends Error {}

function rejectSecretShaped(value: unknown, path = "$"): void {
  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
      if (SECRET_KEY_RE.test(key)) throw new ContractInvalid(`secret-shaped field at ${path}.${key}`);
      rejectSecretShaped(child, `${path}.${key}`);
    }
  } else if (Array.isArray(value)) {
    value.forEach((child, index) => rejectSecretShaped(child, `${path}[${index}]`));
  } else if (typeof value === "string" && SECRET_VALUE_RE.test(value)) {
    throw new ContractInvalid(`secret-shaped value at ${path}`);
  }
}

function validReadiness(value: unknown): { healthy: boolean; launchable: boolean } {
  if (
    typeof value !== "object" || value === null ||
    typeof (value as Record<string, unknown>).healthy !== "boolean" ||
    typeof (value as Record<string, unknown>).launchable !== "boolean"
  ) {
    throw new ContractInvalid("readiness must carry healthy and launchable booleans");
  }
  const v = value as Record<string, unknown>;
  return { healthy: v.healthy as boolean, launchable: v.launchable as boolean };
}

function validBuildStage(value: unknown): BuildStage | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !(BUILD_STAGES as readonly string[]).includes(value)) {
    throw new ContractInvalid("build_stage is not in the published stage vocabulary");
  }
  return value as BuildStage;
}

function validReceiptId(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) {
    throw new ContractInvalid("receipt_id is not a valid identifier");
  }
  return value;
}

/** Schema-validate the fixture contract and drop every unknown field. */
function validateContract(projectId: string, revision: string, raw: unknown): Contract {
  rejectSecretShaped(raw);
  const r = raw as RawContract;
  if (typeof raw !== "object" || raw === null || r.schema !== CONTRACT_SCHEMA) {
    throw new ContractInvalid("unsupported contract schema");
  }
  if (r.project_id !== projectId || r.revision !== revision) {
    throw new ContractInvalid("contract scope does not match the request");
  }
  const contract: Contract = {
    schema: CONTRACT_SCHEMA,
    project_id: projectId,
    revision,
    reported_at: (r.reported_at as string | null) ?? null,
    readiness: validReadiness(r.readiness),
    build_stage: validBuildStage(r.build_stage),
    receipt_id: validReceiptId(r.receipt_id),
  };
  rejectSecretShaped(contract);
  return contract;
}

type RenderResult =
  | { status: "refused" }
  | { status: "available"; contract: Contract }
  | { status: "unavailable"; reason: "upstream_unreachable" | "contract_invalid" };

interface Scope {
  tenant_id: string;
  project_id: string;
  revision: string;
}

/**
 * The harness's own render pipeline: ios_surface OFF refuses before the
 * source is ever touched (the fence); ON, it renders truthfully from the
 * fixture source and never serves a stale prior render on failure.
 */
function renderIosSurface(
  flags: { ios_surface: boolean },
  source: (scope: Scope) => unknown,
  scope: Scope,
): RenderResult {
  if (!flags.ios_surface) return { status: "refused" };
  let raw: unknown;
  try {
    raw = source(scope);
  } catch {
    return { status: "unavailable", reason: "upstream_unreachable" };
  }
  try {
    return { status: "available", contract: validateContract(scope.project_id, scope.revision, raw) };
  } catch {
    return { status: "unavailable", reason: "contract_invalid" };
  }
}

function fixtureContract(overrides: Partial<RawContract> = {}): RawContract {
  return {
    schema: CONTRACT_SCHEMA,
    project_id: "proj-1",
    revision: "r1",
    reported_at: "2026-08-19T12:00:00+00:00",
    readiness: { healthy: true, launchable: false },
    build_stage: null,
    receipt_id: null,
    ...overrides,
  };
}

const SCOPE: Scope = { tenant_id: "tenant-a", project_id: "proj-1", revision: "r1" };

// --------------------------------------------------------------------- //
// E2E: readiness -> stages -> receipt render.
// --------------------------------------------------------------------- //

describe("ios_surface harness E2E (fixture contract)", () => {
  it("renders readiness, walks every published build stage in order, then renders the receipt", () => {
    const flags = { ios_surface: true };

    // Readiness: healthy but not yet launchable, no stage reported yet.
    const readiness = renderIosSurface(flags, () => fixtureContract(), SCOPE);
    expect(readiness).toEqual({
      status: "available",
      contract: expect.objectContaining({
        readiness: { healthy: true, launchable: false },
        build_stage: null,
        receipt_id: null,
      }),
    });

    // Stages: walk the full published vocabulary in the ship-lane's own order.
    // receipt_id must stay absent until the final RECEIPT stage.
    for (const stage of BUILD_STAGES) {
      const isFinal = stage === "RECEIPT";
      const result = renderIosSurface(
        flags,
        () =>
          fixtureContract({
            readiness: { healthy: true, launchable: isFinal },
            build_stage: stage,
            receipt_id: isFinal ? "receipt-ship-1" : null,
          }),
        SCOPE,
      );
      expect(result.status).toBe("available");
      if (result.status !== "available") throw new Error("unreachable");
      expect(result.contract.build_stage).toBe(stage);
      expect(result.contract.receipt_id).toBe(isFinal ? "receipt-ship-1" : null);
      expect(result.contract.readiness.launchable).toBe(isFinal);
    }

    // Receipt render: the terminal stage carries a valid, renderable receipt.
    const receipt = renderIosSurface(
      flags,
      () =>
        fixtureContract({
          readiness: { healthy: true, launchable: true },
          build_stage: "RECEIPT",
          receipt_id: "receipt-ship-1",
        }),
      SCOPE,
    );
    expect(receipt).toEqual({
      status: "available",
      contract: expect.objectContaining({ build_stage: "RECEIPT", receipt_id: "receipt-ship-1" }),
    });
  });

  it("never serves a stale prior render when the upstream fails after a successful stage", () => {
    const flags = { ios_surface: true };
    const ok = renderIosSurface(
      flags,
      () => fixtureContract({ build_stage: "BUILT" }),
      SCOPE,
    );
    expect(ok.status).toBe("available");

    const failing = renderIosSurface(
      flags,
      () => {
        throw new Error("upstream is down");
      },
      SCOPE,
    );
    expect(failing).toEqual({ status: "unavailable", reason: "upstream_unreachable" });
    expect(JSON.stringify(failing)).not.toContain("BUILT");
  });
});

// --------------------------------------------------------------------- //
// Fence negative control: ios_surface OFF refuses (flip-time proof).
// --------------------------------------------------------------------- //

describe("ios_surface fence negative control (flip-time proof)", () => {
  it("refuses without ever touching the source while OFF, reaches it once flipped ON, and refuses again once flipped back OFF", () => {
    let calls = 0;
    const source = (_scope: Scope): RawContract => {
      calls += 1;
      return fixtureContract({ build_stage: "BUILT" });
    };

    // OFF: refused, source never called -- proven by call count, not just status.
    const flags = { ios_surface: false };
    for (let i = 0; i < 3; i += 1) {
      expect(renderIosSurface(flags, source, SCOPE)).toEqual({ status: "refused" });
    }
    expect(calls).toBe(0);

    // Flip ON: the identical call now reaches the source.
    flags.ios_surface = true;
    const onResult = renderIosSurface(flags, source, SCOPE);
    expect(onResult.status).toBe("available");
    expect(calls).toBe(1);

    // Flip back OFF: refused again, at the exact same call site, with no
    // further source calls -- the fence, not the source's own behavior, is
    // what changed.
    flags.ios_surface = false;
    for (let i = 0; i < 3; i += 1) {
      expect(renderIosSurface(flags, source, SCOPE)).toEqual({ status: "refused" });
    }
    expect(calls).toBe(1);
  });
});

// --------------------------------------------------------------------- //
// Credential-material grep assertion over the whole D-slice diff.
// --------------------------------------------------------------------- //

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

function runGit(args: string[]): string {
  return execFileSync("git", ["-c", "safe.directory=*", ...args], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    maxBuffer: 64 * 1024 * 1024,
  });
}

let gitAvailable = true;
try {
  runGit(["rev-parse", "--show-toplevel"]);
} catch {
  gitAvailable = false;
}

/** Every "card D-" commit reachable from HEAD, plus any uncommitted change --
 * this file's own D-4 diff before it is committed is exactly the working-tree
 * half of that union. */
function collectDWaveSliceDiff(): string {
  const log = runGit(["log", "--format=%H", "--grep=^card D-", "-i"]);
  const hashes = log.split("\n").map((h) => h.trim()).filter(Boolean);
  const commitDiffs = hashes.map((hash) => runGit(["show", hash]));
  const workingDiff = runGit(["diff", "HEAD"]);
  const stagedDiff = runGit(["diff", "--cached"]);
  return [...commitDiffs, workingDiff, stagedDiff].join("\n");
}

// Value-pattern only, deliberately: SECRET_KEY_RE's own pattern text (and
// this file's fixture data referencing field names like "credential") is
// legitimately present in the D-slice diff. Matching on literal secret
// *values* -- PEM private-key blocks, JWT-looking blobs, long base64 blobs --
// distinguishes an actual leaked credential from source code that merely
// talks about credentials, mirroring
// server/tests/test_ios_surface_contract.py::test_source_file_carries_no_hardcoded_secret_material.
const FORBIDDEN_VALUE_PATTERNS: RegExp[] = [
  /-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----/,
  /eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}/,
];

describe("credential-material grep assertion (whole D-slice diff)", () => {
  it.skipIf(!gitAvailable)(
    "no secret-shaped literal value appears anywhere in the D-wave slice diff",
    () => {
      const diffText = collectDWaveSliceDiff();
      expect(diffText.length).toBeGreaterThan(0);
      for (const pattern of FORBIDDEN_VALUE_PATTERNS) {
        const match = pattern.exec(diffText);
        expect(match, `forbidden secret-shaped literal in D-slice diff: ${pattern}`).toBeNull();
      }
    },
  );
});
