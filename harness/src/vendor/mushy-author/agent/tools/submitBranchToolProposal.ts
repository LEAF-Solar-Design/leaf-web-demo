/** Structured branch submission: validate everything before publishing a package. */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  BranchPackageError, prepareBranchPackage, validateBranchPackage,
  type BranchManifest,
} from "../../registry/branchPackageSchema.js";
import { FsTenantRepo } from "./fsTenantRepo.js";
import { isWindowsUnsafeRepoComponent, resolveContainedRepoPath, revalidateResolvedRepoPath } from "./repoPathSafety.js";

export interface BranchSubmissionResult {
  manifest: BranchManifest;
  package_path: string;
  files: string[];
  /** Exact content retained by the trusted author loop for post-run verification. */
  contents: Record<string, string>;
  package_digest: string;
}

export function branchPackageDigest(files: Record<string, string>): string {
  const lines = Object.keys(files).sort().map(path => {
    const bytes = Buffer.from(files[path], "utf8");
    return `${path}\t${bytes.length}\t${createHash("sha256").update(bytes).digest("hex")}\n`;
  }).join("");
  return createHash("sha256").update(lines).digest("hex");
}

export function submitBranchToolProposal(
  repoDir: string,
  proposal: unknown,
  options: { session?: string; now?: Date; targetToolName?: string } = {},
): BranchSubmissionResult {
  const { manifest, files } = prepareBranchPackage(proposal, options.session ?? "agent-sdk", options.now);
  const refuse = (rule: string, message: string): never => {
    throw new BranchPackageError([{ rule, path: `branch-tools/${manifest.name}`, message }]);
  };
  for (const path of [manifest.name, ...Object.keys(files).map(p => p.split("/").at(-1)!)]) {
    if (isWindowsUnsafeRepoComponent(path)) refuse("V16", "unsafe Windows file name");
  }
  const fs = new FsTenantRepo(repoDir);
  const packagePath = `branch-tools/${manifest.name}`;
  const parent = resolveContainedRepoPath(fs.root, "branch-tools");
  const target = resolveContainedRepoPath(fs.root, packagePath);
  if (!parent || !target) refuse("V16", "unsafe package path");
  if (options.targetToolName !== undefined && options.targetToolName !== manifest.name) refuse("V3", "revision must keep its bound package name");
  if (existsSync(parent!.path)) {
    const collision = readdirSync(parent!.path).find(n => n.toLowerCase() === manifest.name.toLowerCase());
    if (collision !== undefined && (options.targetToolName === undefined || collision !== manifest.name)) refuse("V3", "package already exists");
  }
  const replacing = options.targetToolName !== undefined;
  if (replacing) {
    if (!fs.exists(`${packagePath}/tool.json`)) refuse("V3", "revision target is missing");
    const previous = JSON.parse(fs.readFile(`${packagePath}/tool.json`)) as BranchManifest;
    if (previous.name !== manifest.name || previous.provenance?.author !== "agent" || previous.entry !== "main.csx") refuse("V3", "revision target is not an authored branch package");
    const before = previous.version.split(".");
    const after = manifest.version.split(".");
    if (before.length !== 3 || before.some(s => !/^(0|[1-9][0-9]*)(?![\s\S])/.test(s))) refuse("V4", "revision target has an invalid version");
    const compare = after.map((s, i) => BigInt(s) > BigInt(before[i]) ? 1 : BigInt(s) < BigInt(before[i]) ? -1 : 0).find(n => n !== 0) ?? 0;
    if (compare <= 0) refuse("V4", "revision version must increase");
    manifest.provenance.created = previous.provenance.created;
    files["tool.json"] = JSON.stringify(manifest, null, 2) + "\n";
    const diagnostics = validateBranchPackage(manifest, files, manifest.name);
    if (diagnostics.length) throw new BranchPackageError(diagnostics);
  } else if (fs.exists(packagePath)) refuse("V3", "package already exists");

  // Build the entire candidate before renaming it into place, retaining a
  // backup during replacement. Temporary paths never enter the commit.
  revalidateResolvedRepoPath(parent!);
  revalidateResolvedRepoPath(target!);
  mkdirSync(parent!.path, { recursive: true });
  const staging = mkdtempSync(join(parent!.path, ".branch-package-"));
  const candidate = join(staging, "candidate");
  const backup = join(staging, "previous");
  let backedUp = false;
  try {
    mkdirSync(candidate);
    for (const [path, content] of Object.entries(files)) {
      if (path.startsWith("lib/")) mkdirSync(join(candidate, "lib"), { recursive: true });
      writeFileSync(join(candidate, path), content, { encoding: "utf8", flag: "wx" });
    }
    const current = resolveContainedRepoPath(fs.root, packagePath);
    if (!current || (replacing ? !existsSync(current.path) : existsSync(current.path))) refuse("V16", "package path changed before publication");
    if (replacing) {
      revalidateResolvedRepoPath(target!);
      renameSync(current!.path, backup);
      backedUp = true;
    }
    try { renameSync(candidate, current!.path); }
    catch (error) {
      if (backedUp) { renameSync(backup, current!.path); backedUp = false; }
      throw error;
    }
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
  return {
    manifest, package_path: packagePath,
    files: Object.keys(files).map(path => `${packagePath}/${path}`).sort(),
    contents: files, package_digest: branchPackageDigest(files),
  };
}
