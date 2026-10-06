/**
 * Trusted structured author boundary.
 *
 * The model supplies source bytes and manifest metadata through one typed call.
 * This module validates the proposal, rejects collisions, and atomically
 * publishes exactly two files under tools/<name>/. Generated code is never
 * imported or executed here. Its first execution remains the broker test run.
 */

import { createHash } from "node:crypto";
import { withFailureCategory } from "../captureGuards.js";
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
import { checkViewFragment, VIEW_ENTRY_BASENAME } from "../../registry/viewChecks.js";

const KEBAB = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const ENGINE_OP = /^[a-z][a-z0-9_]{0,63}$/;
const RUN_FUNCTION = /(?:^|\n)[ \t]*def[ \t]+run[ \t]*\([ \t]*intake[ \t]*,[ \t]*params[ \t]*\)[ \t]*:/;
// ASCII digits only, at most CPython's default int_max_str_digits (4300) per
// group: the server reads a version with Python's `\d`, whose Unicode table
// differs between Python and Node releases, and converts the incremented group
// with int() and str(), which raise past 4300 digits. A version outside this
// grammar is refused here even where one server build would accept it.
export const MAX_VERSION_GROUP_DIGITS = 4300;
const SEMVER = /^([0-9]{1,4300})\.([0-9]{1,4300})\.([0-9]{1,4300})$/;
export const MAX_TOOL_SOURCE_BYTES = 512 * 1024;

export const REVISION_ERROR = "invalid_staged_catalog_revision";

class JsonNumber {
  constructor(readonly token: string) {}
}

type JsonNode =
  | null | boolean | string | JsonNumber
  | JsonNode[] | Map<string, JsonNode>;

function lossless(text: string): JsonNode {
  const quoted = text.replace(
    /"(?:\\[\s\S]|[^"\\])*"|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?/g,
    token => JSON.stringify(
      token[0] === '"' ? "s:" + JSON.parse(token) : "n:" + token,
    ),
  );
  function lift(value: unknown): JsonNode {
    if (typeof value === "string") {
      return value.startsWith("s:")
        ? value.slice(2) : new JsonNumber(value.slice(2));
    }
    if (value === null || typeof value === "boolean") return value;
    if (Array.isArray(value)) return value.map(lift);
    if (typeof value === "object") {
      return new Map(Object.entries(value).map(([key, child]) => {
        if (!key.startsWith("s:")) throw new Error(REVISION_ERROR);
        return [key.slice(2), lift(child)] as [string, JsonNode];
      }));
    }
    throw new Error(REVISION_ERROR);
  }
  return lift(JSON.parse(quoted));
}

function numberKey(token: string): string {
  if (!/[.eE]/.test(token)) return "i:" + BigInt(token);
  const match = /^(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$/.exec(token);
  if (!match) throw new Error(REVISION_ERROR);
  const sign = match[1];
  const digits = (match[2] + (match[3] ?? "")).replace(/^0+/, "");
  if (!digits) return "f:" + sign + "0";
  const scale = BigInt(match[4] ?? "0") - BigInt((match[3] ?? "").length);
  const magnitude = BigInt(digits.length) - 1n + scale;
  if (magnitude > 308n) return "f:" + sign + "inf";
  if (magnitude < -324n) return "f:" + sign + "0";

  let numerator = BigInt(digits);
  let denominator = 1n;
  if (scale >= 0n) numerator *= 10n ** scale;
  else denominator = 10n ** (-scale);

  let exponent = BigInt(
    numerator.toString(2).length - denominator.toString(2).length,
  );
  if (
    exponent >= 0n
      ? numerator < (denominator << exponent)
      : (numerator << (-exponent)) < denominator
  ) exponent -= 1n;

  let power = exponent - 52n;
  if (power < -1074n) power = -1074n;
  if (power >= 0n) denominator <<= power;
  else numerator <<= -power;

  let significand = numerator / denominator;
  const remainder = numerator % denominator;
  if (
    2n * remainder > denominator ||
    (2n * remainder === denominator && significand % 2n === 1n)
  ) significand += 1n;

  if (significand === 0n) return "f:" + sign + "0";
  if (significand === 2n ** 53n) {
    significand /= 2n;
    power += 1n;
  }
  if (power > 971n) return "f:" + sign + "inf";
  return "f:" + sign + significand + "p" + power;
}

function schemaEqual(
  left: JsonNode | undefined,
  right: JsonNode | undefined,
  depth = 0,
): boolean {
  if (depth > 128) return false;
  if (left instanceof JsonNumber || right instanceof JsonNumber) {
    return left instanceof JsonNumber && right instanceof JsonNumber &&
      numberKey(left.token) === numberKey(right.token);
  }
  if (left instanceof Map || right instanceof Map) {
    return left instanceof Map && right instanceof Map &&
      left.size === right.size &&
      [...left].every(([key, value]) =>
        right.has(key) && schemaEqual(value, right.get(key), depth + 1));
  }
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) &&
      left.length === right.length &&
      left.every((value, index) => schemaEqual(value, right[index], depth + 1));
  }
  return left === right;
}

type SchemaChange = "same" | "additive" | "breaking";

function classifySchema(base: JsonNode, staged: JsonNode): SchemaChange {
  if (schemaEqual(base, staged)) return "same";
  if (
    !(base instanceof Map) || !(staged instanceof Map) ||
    base.get("type") !== "object" || staged.get("type") !== "object"
  ) return "breaking";

  const rest = (value: Map<string, JsonNode>) =>
    new Map([...value].filter(([key]) => key !== "properties"));
  if (!schemaEqual(rest(base), rest(staged))) return "breaking";

  const before = base.has("properties") ? base.get("properties") : new Map();
  const after = staged.has("properties") ? staged.get("properties") : new Map();
  if (!(before instanceof Map) || !(after instanceof Map)) return "breaking";

  if (base.has("required")) {
    const required = base.get("required");
    if (
      !Array.isArray(required) ||
      required.some(key => typeof key !== "string" || !before.has(key)) ||
      new Set(required).size !== required.length
    ) return "breaking";
  }
  if ([...before].some(([key, value]) =>
    !after.has(key) || !schemaEqual(value, after.get(key)))) return "breaking";

  const added = [...after.keys()].filter(key => !before.has(key));
  if (
    !added.length ||
    added.some(key =>
      !(after.get(key) instanceof Map) && typeof after.get(key) !== "boolean")
  ) return "breaking";
  return "additive";
}

export function revisionSchemaChange(
  baseJson: string,
  stagedJson: string,
): SchemaChange {
  return classifySchema(lossless(baseJson), lossless(stagedJson));
}

function incrementDecimal(value: string): string {
  const next = (BigInt(value) + 1n).toString();
  if (next.length > MAX_VERSION_GROUP_DIGITS) throw new Error(REVISION_ERROR);
  return next;
}

export function revisionVersion(
  rawRegistry: string,
  targetName: string,
  candidateJson: string,
): string {
  try {
    const registry = lossless(rawRegistry);
    const candidate = lossless(candidateJson);
    const tools = registry instanceof Map ? registry.get("tools") : null;
    if (!Array.isArray(tools) || !(candidate instanceof Map)) {
      throw new Error(REVISION_ERROR);
    }
    const matches = tools.filter(
      (tool): tool is Map<string, JsonNode> =>
        tool instanceof Map && tool.get("name") === targetName,
    );
    if (matches.length !== 1) throw new Error(REVISION_ERROR);
    const base = matches[0]!;
    const changes = ["params", "returns"].map(key =>
      classifySchema(base.get(key) ?? null, candidate.get(key) ?? null));
    const version = base.get("version");
    const match = typeof version === "string" ? SEMVER.exec(version) : null;
    if (
      changes.includes("breaking") || !match || match[0] !== version
    ) throw new Error(REVISION_ERROR);
    return changes.includes("additive")
      ? `${match[1]}.${incrementDecimal(match[2]!)}.0`
      : `${match[1]}.${match[2]}.${incrementDecimal(match[3]!)}`;
  } catch {
    throw new Error(REVISION_ERROR);
  }
}

function sha256(bytes: Buffer | string): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function assertInside(root: string, path: string): void {
  const rel = relative(root, path);
  if (rel === ".." || rel.startsWith(`..${sep}`) || rel.startsWith(sep)) {
    throw withFailureCategory(new Error("tool proposal target escapes the tenant repository"), "validation_failed");
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
  entryBasename: string,
): string {
  if (previous.entry !== entry || previous.manifest !== manifestPath) {
    throw withFailureCategory(new Error("tool proposal replacement receipt path mismatch"), "validation_failed");
  }
  assertOrdinaryDirectory(targetDir, "existing proposal package");
  const names = readdirSync(targetDir).sort();
  if (JSON.stringify(names) !== JSON.stringify([entryBasename, "tool.json"].sort())) {
    throw withFailureCategory(new Error("tool proposal replacement target contains unexpected files"), "validation_failed");
  }
  for (const name of names) {
    const stat = lstatSync(join(targetDir, name));
    if (stat.isSymbolicLink() || !stat.isFile()) {
      throw withFailureCategory(new Error("tool proposal replacement target must contain ordinary files"), "validation_failed");
    }
  }
  const source = readFileSync(join(targetDir, entryBasename));
  const manifest = readFileSync(join(targetDir, "tool.json"));
  if (
    source.byteLength !== previous.source_bytes ||
    manifest.byteLength !== previous.manifest_bytes ||
    sha256(source) !== previous.source_sha256 ||
    sha256(manifest) !== previous.manifest_sha256
  ) {
    throw withFailureCategory(new Error("tool proposal replacement receipt does not match existing bytes"), "validation_failed");
  }
  try {
    const parsed = JSON.parse(manifest.toString("utf8")) as ToolPackage;
    const created = parsed.provenance?.created;
    return typeof created === "string" && created ? created : fallbackCreated;
  } catch {
    throw withFailureCategory(new Error("tool proposal replacement manifest is not valid JSON"), "validation_failed");
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
  const candidateJson = JSON.stringify(proposal);
  if (candidateJson === undefined) throw new Error(REVISION_ERROR);
  proposal = JSON.parse(candidateJson) as ToolSourceProposal;
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
  const kind = proposal.kind ?? "script";
  const sourceBytes = Buffer.byteLength(proposal.source, "utf8");
  if (sourceBytes === 0 || sourceBytes > MAX_TOOL_SOURCE_BYTES) {
    diagnostics.push(`source must be 1-${MAX_TOOL_SOURCE_BYTES} UTF-8 bytes`);
  }
  if (proposal.source.includes("\0")) {
    diagnostics.push("source cannot contain NUL bytes");
  }
  if (kind === "view") {
    // The fragment half of validate-tool: a bad widget must not reach a commit.
    diagnostics.push(...checkViewFragment(proposal.source));
  } else if (!RUN_FUNCTION.test(proposal.source)) {
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

  const entryBasename = kind === "view" ? VIEW_ENTRY_BASENAME : "tool.py";
  const entry = `tools/${proposal.name}/${entryBasename}`;
  const manifestPath = `tools/${proposal.name}/tool.json`;
  const modified = now.toISOString();
  const created = existsSync(targetDir)
    ? assertReplaceable(targetDir, previous!, entry, manifestPath, modified, entryBasename)
    : modified;
  const version = registered.length === 1
    ? revisionVersion(
        readFileSync(join(root, "registry.json"), "utf8"),
        proposal.name,
        candidateJson,
      )
    : "1.0.0";
  const tool: ToolPackage = {
    name: proposal.name,
    version,
    description: proposal.description,
    kind,
    engine_op: proposal.engine_op,
    entry,
    params: proposal.params,
    returns: proposal.returns,
    capabilities: [...proposal.capabilities],
    timeout_ms: 30_000,
    idempotent: true,
    review: { status: "unreviewed" },
    // source_ref (non-negotiable 4): harness-derived from the TRUSTED session
    // label, never model-settable free text.
    ...(kind === "view" ? { source_ref: `session:${proposal.session}` } : {}),
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

  const manifest = JSON.stringify({ ...tool, entry: entryBasename }, null, 2) + "\n";
  const tempDir = mkdtempSync(join(toolsDir, ".leaf-proposal-"));
  try {
    writeExact(join(tempDir, entryBasename), proposal.source);
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
