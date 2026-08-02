/**
 * Trusted structured author boundary.
 *
 * The model supplies source bytes and manifest metadata through one typed call.
 * This module validates the proposal, rejects collisions, and atomically
 * publishes exactly two files under tools/<name>/. Generated code is never
 * imported or executed here. Its first execution remains the broker test run.
 */

import { createHash } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { join, relative, resolve, sep } from "node:path";
import type {
  ToolPackage,
  ToolSourceProposal,
  ToolSourceReceipt,
  ToolSubmissionResult,
} from "../../ports/index.js";
import { readRegistry } from "../../registry/registerTool.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";

const KEBAB = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const ENGINE_OP = /^[a-z][a-z0-9_]{0,63}$/;
const RUN_FUNCTION = /(?:^|\n)[ \t]*def[ \t]+run[ \t]*\([ \t]*intake[ \t]*,[ \t]*params[ \t]*\)[ \t]*:/;
const SEMVER = /^(\d+)\.(\d+)\.(\d+)$/;
export const MAX_TOOL_SOURCE_BYTES = 512 * 1024;

function sha256(bytes: Buffer | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function assertInside(root: string, path: string): void {
  const rel = relative(root, path);
  if (rel === ".." || rel.startsWith(`..${sep}`) || rel.startsWith(sep)) {
    throw new Error("tool proposal target escapes the tenant repository");
  }
}

function assertOrdinaryDirectory(path: string, label: string): void {
  const stat = lstatSync(path);
  if (stat.isSymbolicLink() || !stat.isDirectory()) {
    throw new Error(`${label} must be an ordinary directory`);
  }
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

function assertReplaceable(
  targetDir: string,
  previous: ToolSourceReceipt,
  entry: string,
  manifestPath: string,
  fallbackCreated: string,
): string {
  if (previous.entry !== entry || previous.manifest !== manifestPath) {
    throw new Error("tool proposal replacement receipt path mismatch");
  }
  assertOrdinaryDirectory(targetDir, "existing proposal package");
  const names = readdirSync(targetDir).sort();
  if (JSON.stringify(names) !== JSON.stringify(["tool.json", "tool.py"])) {
    throw new Error("tool proposal replacement target contains unexpected files");
  }
  for (const name of names) {
    const stat = lstatSync(join(targetDir, name));
    if (stat.isSymbolicLink() || !stat.isFile()) {
      throw new Error("tool proposal replacement target must contain ordinary files");
    }
  }
  const source = readFileSync(join(targetDir, "tool.py"));
  const manifest = readFileSync(join(targetDir, "tool.json"));
  if (
    source.byteLength !== previous.source_bytes ||
    manifest.byteLength !== previous.manifest_bytes ||
    sha256(source) !== previous.source_sha256 ||
    sha256(manifest) !== previous.manifest_sha256
  ) {
    throw new Error("tool proposal replacement receipt does not match existing bytes");
  }
  try {
    const parsed = JSON.parse(manifest.toString("utf8")) as ToolPackage;
    const created = parsed.provenance?.created;
    return typeof created === "string" && created ? created : fallbackCreated;
  } catch {
    throw new Error("tool proposal replacement manifest is not valid JSON");
  }
}

function validateWriteContract(proposal: ToolSourceProposal, diagnostics: string[]): void {
  if (!proposal.capabilities.includes("drawing.write")) return;
  const properties = proposal.params.properties;
  const drawingId = properties?.drawing_id as Record<string, unknown> | undefined;
  const dryRun = properties?.dry_run as Record<string, unknown> | undefined;
  if (drawingId?.type !== "string") {
    diagnostics.push('drawing.write params must define drawing_id with type "string"');
  }
  if (dryRun?.type !== "boolean" || dryRun.default !== false) {
    diagnostics.push('drawing.write params must define dry_run with type "boolean" and default false');
  }
}

export function submitToolProposal(
  repoDir: string,
  proposal: ToolSourceProposal,
  now = new Date(),
  previous?: ToolSourceReceipt,
): ToolSubmissionResult {
  const diagnostics: string[] = [];
  if (!KEBAB.test(proposal.name) || proposal.name.length > 64) {
    diagnostics.push("name must be 1-64 lowercase kebab-case characters");
  }
  if (!ENGINE_OP.test(proposal.engine_op)) {
    diagnostics.push("engine_op must be 1-64 lowercase snake_case characters");
  }
  if (!proposal.description.trim() || proposal.description.length > 1000) {
    diagnostics.push("description must be 1-1000 characters");
  }
  const sourceBytes = Buffer.byteLength(proposal.source, "utf8");
  if (sourceBytes === 0 || sourceBytes > MAX_TOOL_SOURCE_BYTES) {
    diagnostics.push(`source must be 1-${MAX_TOOL_SOURCE_BYTES} UTF-8 bytes`);
  }
  if (proposal.source.includes("\0")) {
    diagnostics.push("source cannot contain NUL bytes");
  }
  if (!RUN_FUNCTION.test(proposal.source)) {
    diagnostics.push("source must define def run(intake, params):");
  }
  validateWriteContract(proposal, diagnostics);
  if (diagnostics.length > 0) {
    throw new Error(`tool proposal rejected: ${diagnostics.join("; ")}`);
  }

  const root = realpathSync(resolve(repoDir));
  const toolsDir = join(root, "tools");
  assertInside(root, toolsDir);
  if (existsSync(toolsDir)) {
    assertOrdinaryDirectory(toolsDir, "tools");
  } else {
    mkdirSync(toolsDir);
  }

  const registry = readRegistry(root);
  const registered = registry.tools.filter((tool) => tool.name === proposal.name);
  if (registered.length > 1) {
    throw new Error(`tool proposal rejected: multiple ${JSON.stringify(proposal.name)} entries exist`);
  }
  if (registered.length === 1 && !previous) {
    throw new Error(`tool proposal rejected: ${JSON.stringify(proposal.name)} already exists`);
  }

  const targetDir = join(toolsDir, proposal.name);
  assertInside(root, targetDir);
  if (existsSync(targetDir) && !previous) {
    throw new Error(`tool proposal rejected: package path for ${JSON.stringify(proposal.name)} already exists`);
  }

  const entry = `tools/${proposal.name}/tool.py`;
  const manifestPath = `tools/${proposal.name}/tool.json`;
  const modified = now.toISOString();
  const created = existsSync(targetDir)
    ? assertReplaceable(targetDir, previous!, entry, manifestPath, modified)
    : modified;
  let version = "1.0.0";
  if (registered.length === 1) {
    const match = SEMVER.exec(registered[0]!.version);
    if (!match || Number(match[3]) >= Number.MAX_SAFE_INTEGER) {
      throw new Error("tool proposal rejected: revision target has an unsupported version");
    }
    version = `${match[1]}.${match[2]}.${Number(match[3]) + 1}`;
  }
  const tool: ToolPackage = {
    name: proposal.name,
    version,
    description: proposal.description,
    kind: "script",
    engine_op: proposal.engine_op,
    entry,
    params: proposal.params,
    returns: proposal.returns,
    capabilities: [...proposal.capabilities],
    timeout_ms: 30_000,
    idempotent: true,
    review: { status: "unreviewed" },
    provenance: {
      author: "agent",
      created,
      modified,
      session: proposal.session,
      static_scan: [],
    },
  };
  const packageDiagnostics = validateToolPackage(tool);
  if (packageDiagnostics.length > 0) {
    throw new Error(`tool proposal rejected: ${packageDiagnostics.join("; ")}`);
  }

  const manifest = JSON.stringify({ ...tool, entry: "tool.py" }, null, 2) + "\n";
  const tempDir = mkdtempSync(join(toolsDir, ".leaf-proposal-"));
  try {
    writeExact(join(tempDir, "tool.py"), proposal.source);
    writeExact(join(tempDir, "tool.json"), manifest);
    if (!existsSync(targetDir)) {
      renameSync(tempDir, targetDir);
    } else {
      const swapDir = mkdtempSync(join(toolsDir, ".leaf-replace-"));
      const oldDir = join(swapDir, "old");
      let oldMoved = false;
      try {
        renameSync(targetDir, oldDir);
        oldMoved = true;
        try {
          renameSync(tempDir, targetDir);
        } catch (error) {
          try {
            renameSync(oldDir, targetDir);
            oldMoved = false;
          } catch {
            // Preserve the old package in swapDir. Deleting it here would turn
            // a failed replacement into data loss. The unexpected directory
            // also makes AuthorLoop's exact-diff check fail closed.
          }
          throw error;
        }
        oldMoved = false;
        rmSync(swapDir, { recursive: true, force: true });
      } catch (error) {
        if (!oldMoved) {
          rmSync(swapDir, { recursive: true, force: true });
        }
        throw error;
      }
    }
  } catch (error) {
    rmSync(tempDir, { recursive: true, force: true });
    throw error;
  }

  return {
    tool,
    code: proposal.source,
    files: [manifestPath, entry],
    receipt: {
      contract: "leaf.tool-source.v1",
      source_sha256: sha256(proposal.source),
      manifest_sha256: sha256(manifest),
      source_bytes: sourceBytes,
      manifest_bytes: Buffer.byteLength(manifest, "utf8"),
      entry,
      manifest: manifestPath,
    },
  };
}
