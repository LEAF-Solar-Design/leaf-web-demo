/**
 * `submitSurfaceConfig` — the fourth granted author tool (slice 7b).
 *
 * Validates a proposed surface-config overlay against
 * contract/surface-config.v1.schema.json BEFORE any write, refusing anything
 * the schema rejects with a named reason and committing nothing. On success it
 * atomically commits <root>/surface-config.json the way submitToolProposal
 * commits tools/<name>/*: write to a sibling temp file under the repo root,
 * then rename into place, so a crash never leaves load_repo_surface_config a
 * partially-written file to read.
 *
 * Re-validation before commit: the overlay is re-validated from the EXACT
 * bytes about to be written (parsed back from the serialized JSON), not from
 * the caller-supplied object a second time. That is what catches a proposal
 * that reports a clean shape once and something else on a later read (a
 * getter, a stateful toJSON) — the mutation-before-commit case this tool must
 * refuse rather than trust.
 */

import { createHash } from "node:crypto";
import { closeSync, fsyncSync, mkdtempSync, openSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import type {
  SurfaceConfigProposal,
  SurfaceConfigReceipt,
  SurfaceConfigSubmissionResult,
} from "../../ports/index.js";
import { withFailureCategory } from "../captureGuards.js";
import { resolveContainedRepoPath, revalidateResolvedRepoPath } from "./repoPathSafety.js";
import { validateSurfaceConfig } from "./surfaceConfigSchema.js";

export const SURFACE_CONFIG_FILE = "surface-config.json";

// Mirrors mushy_fold.surface_config.MAX_SURFACE_CONFIG_BYTES exactly: the fold
// reader and this writer must agree on the bound, or a file this tool commits
// as valid could be silently dropped by the fold as oversize.
export const MAX_SURFACE_CONFIG_BYTES = 64 * 1024;

function sha256(bytes: Buffer | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function writeExact(path: string, content: string): void {
  const fd = openSync(path, "wx", 0o600);
  try {
    writeFileSync(fd, content, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function submitSurfaceConfig(
  repoDir: string,
  proposal: SurfaceConfigProposal,
): SurfaceConfigSubmissionResult {
  const diagnostics = validateSurfaceConfig(proposal.overlay);
  if (diagnostics.length > 0) {
    throw withFailureCategory(
      new Error(`surface config rejected: ${diagnostics.join("; ")}`),
      "validation_failed",
    );
  }

  // Serialize ONCE, off the caller-owned object. Everything from here reads
  // only `content` / `reparsed`, never `proposal.overlay` again, so nothing
  // the caller still holds a reference to can change what gets committed.
  const content = JSON.stringify(proposal.overlay, null, 2) + "\n";
  const bytes = Buffer.byteLength(content, "utf8");
  if (bytes > MAX_SURFACE_CONFIG_BYTES) {
    throw withFailureCategory(
      new Error(`surface config rejected: exceeds ${MAX_SURFACE_CONFIG_BYTES} bytes`),
      "validation_failed",
    );
  }

  // Re-validation before commit (TOCTOU guard): re-parse the exact bytes about
  // to be written and validate THAT, not the original object a second time.
  const reparsed: unknown = JSON.parse(content);
  const reDiagnostics = validateSurfaceConfig(reparsed);
  if (reDiagnostics.length > 0) {
    throw withFailureCategory(
      new Error(`surface config rejected on re-validation: ${reDiagnostics.join("; ")}`),
      "validation_failed",
    );
  }

  const root = resolve(repoDir);
  const target = resolveContainedRepoPath(root, SURFACE_CONFIG_FILE);
  if (!target) {
    throw withFailureCategory(
      new Error("surface config target escapes the tenant repository"),
      "validation_failed",
    );
  }

  const tempDir = mkdtempSync(join(root, ".leaf-surface-config-"));
  try {
    const tempFile = join(tempDir, SURFACE_CONFIG_FILE);
    writeExact(tempFile, content);
    // Re-check containment/identity right before the rename: nothing about
    // the target may have changed (e.g. into a symlink) between resolving it
    // above and publishing the written bytes.
    revalidateResolvedRepoPath(target);
    renameSync(tempFile, target.path);
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }

  const receipt: SurfaceConfigReceipt = {
    contract: "leaf.surface-config.v1",
    sha256: sha256(content),
    bytes,
    path: SURFACE_CONFIG_FILE,
  };

  return {
    overlay: reparsed as Record<string, unknown>,
    file: SURFACE_CONFIG_FILE,
    receipt,
  };
}
