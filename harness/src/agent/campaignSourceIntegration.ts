/** Verified campaign product bytes enter the existing project edit transaction here. */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, lstatSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import type { ProjectRepositoryAuthority } from "../ports/index.js";
import { ProjectRepositoryEditError, type ProjectRepositoryEditCoordinator,
  type StageProjectRepositoryEditRequest } from "./projectRepositoryEditCoordinator.js";

export type CampaignSourceService = Pick<ProjectRepositoryEditCoordinator, "stageEdit" | "publishEdit" | "recoverEdit">;
export const CAMPAIGN_SOURCE_BODY_LIMIT = 6291456;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SHA = /^[0-9a-f]{40}$/;
const DIGEST = /^[0-9a-f]{64}$/;
const authorityKeys = ["tenant_id", "organization_id", "project_id", "repo_key"];
const commonKeys = [...authorityKeys, "edit_id", "actor_binding_id"];
function invalid(): never { throw new ProjectRepositoryEditError("invalid_source_request"); }
function closed(value: unknown, keys: string[]): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value) ||
      Object.keys(value).sort().join(",") !== [...keys].sort().join(",")) invalid();
  return value as Record<string, unknown>;
}
function text(value: unknown, max: number, pattern?: RegExp): string {
  if (typeof value !== "string" || !value.length || value.length > max ||
      /[\x00-\x1f\x7f]/.test(value) || (pattern && !pattern.test(value))) invalid();
  return value;
}
function version(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1) invalid();
  return value;
}
function productPath(value: unknown): string {
  const path = text(value, 240);
  const parts = path.split("/");
  const lower = path.toLowerCase();
  if (/[\\:?#%]/.test(path) || parts.some(p => !p || p === "." || p === ".." ||
      p.endsWith(".") || p.endsWith(" ") || /^(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\.|$)/i.test(p)) ||
      parts.some(p => p.toLowerCase() === ".git") ||
      /(^|\/)(\.env(?:\..*)?|\.ssh|\.aws|credentials(?:\..*)?)(\/|$)/i.test(path) ||
      lower === "prompt.md" || lower === ".leaf/source-seed.json" ||
      lower === ".leaf/campaign-plan.json" || lower === "findings/pair" || lower.startsWith("findings/pair/")) invalid();
  return path;
}
const digest = (bytes: Buffer): string => createHash("sha256").update(bytes).digest("hex");
function git(dir: string, args: string[]): Buffer {
  const env = { ...process.env };
  for (const key of Object.keys(env)) if (key.startsWith("GIT_")) delete env[key];
  Object.assign(env, { GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: process.platform === "win32" ? "NUL" : "/dev/null",
    GIT_NO_REPLACE_OBJECTS: "1", GIT_LITERAL_PATHSPECS: "1", LC_ALL: "C" });
  return execFileSync("git", ["-c", "core.autocrlf=false", "-c", "core.quotePath=true",
    "-c", "diff.algorithm=myers", "-c", "diff.indentHeuristic=false", ...args],
  { cwd: dir, env, maxBuffer: 16777216, timeout: 20000, stdio: ["ignore", "pipe", "ignore"] });
}

function stageRequest(body: Record<string, unknown>, authority: ProjectRepositoryAuthority): StageProjectRepositoryEditRequest {
  const expectedBaseCommit = text(body.expected_base_commit, 40, SHA);
  if (!Array.isArray(body.files) || body.files.length < 1 || body.files.length > 64) invalid();
  let total = 0;
  const seen = new Set<string>();
  const files = body.files.map(value => {
    const file = closed(value, ["path", "key", "sha256", "size_bytes", "product_b64", "before_sha256"]);
    const path = productPath(file.path);
    if (seen.has(path.toLowerCase())) invalid();
    seen.add(path.toLowerCase());
    text(file.key, 1024);
    const sha256 = text(file.sha256, 64, DIGEST);
    const before = file.before_sha256 === null ? null : text(file.before_sha256, 64, DIGEST);
    if (typeof file.size_bytes !== "number" || !Number.isSafeInteger(file.size_bytes) ||
        file.size_bytes < 0 || file.size_bytes > 1048576 ||
        typeof file.product_b64 !== "string" || file.product_b64.length > 1398104) invalid();
    const bytes = Buffer.from(file.product_b64, "base64");
    if (bytes.toString("base64") !== file.product_b64 || bytes.length !== file.size_bytes || digest(bytes) !== sha256) invalid();
    total += bytes.length;
    if (total > 4194304) invalid();
    return { path, bytes, before, mode: "100644" };
  });
  // All before hashes and tree modes are checked before the first file write.
  return {
    authority, editId: text(body.edit_id, 36, UUID), actorBindingId: text(body.actor_binding_id, 36, UUID),
    operation: "edit", sourceEditId: null, expectedBaseCommit,
    instructionDigest: text(body.instruction_digest, 64, DIGEST),
    idempotencyKey: text(body.idempotency_key, 200), commitMessage: text(body.commit_message, 512),
    apply(dir) {
      if (git(dir, ["rev-parse", "HEAD"]).toString().trim() !== expectedBaseCommit ||
          git(dir, ["rev-parse", "refs/heads/main"]).toString().trim() !== expectedBaseCommit) {
        throw new ProjectRepositoryEditError("source_base_conflict");
      }
      for (const file of files) {
        const entry = git(dir, ["ls-tree", "-z", expectedBaseCommit, "--", file.path]).toString("utf8");
        if (!entry) { if (file.before !== null) invalid(); }
        else {
          if (!/^(100644|100755) blob [0-9a-f]{40}\t/.test(entry) || file.before === null ||
              digest(git(dir, ["show", `${expectedBaseCommit}:${file.path}`])) !== file.before) invalid();
          file.mode = entry.slice(0, 6);
        }
        let current = dir;
        for (const part of file.path.split("/")) {
          current = join(current, part);
          try {
            const stat = lstatSync(current);
            if (stat.isSymbolicLink() || (current === join(dir, file.path) ? !stat.isFile() : !stat.isDirectory())) invalid();
          } catch (error) {
            if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
          }
        }
      }
      for (const file of files) {
        const target = join(dir, file.path);
        mkdirSync(dirname(target), { recursive: true });
        writeFileSync(target, file.bytes, { mode: 0o644 });
        if (process.platform !== "win32") chmodSync(target, file.mode === "100755" ? 0o755 : 0o644);
      }
    },
    deriveChangeEvidence(dir) {
      git(dir, ["add", "-f", "--", ...files.map(file => file.path)]);
      for (const file of files) {
        git(dir, ["update-index", file.mode === "100755" ? "--chmod=+x" : "--chmod=-x", "--", file.path]);
        if (!git(dir, ["show", `:${file.path}`]).equals(file.bytes)) invalid();
      }
      const args = ["diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-renames"];
      const changedPaths = git(dir, [...args, "--name-only", "-z", expectedBaseCommit, "--"])
        .toString("utf8").split("\0").filter(Boolean);
      if (changedPaths.some(path => !files.some(file => file.path === path))) invalid();
      const diff = git(dir, [...args, "--binary", "--full-index", "--no-color", "--src-prefix=a/", "--dst-prefix=b/",
        "--unified=3", expectedBaseCommit, "--"]);
      return { changedPaths, diffDigest: digest(diff) };
    },
  };
}

export function dispatchCampaignSource(service: CampaignSourceService, action: "stage" | "publish" | "recover",
  value: unknown, tenantHeader: string | undefined) {
  const extra = action === "stage"
    ? ["expected_base_commit", "instruction_digest", "idempotency_key", "commit_message", "files"]
    : action === "publish" ? ["confirmation_id", "receipt_digest", "expected_version", "transition_key"]
      : ["expected_main_commit", "staged_head_commit", "staged_tree", "expected_version", "transition_key", "reason_code"];
  const body = closed(value, [...commonKeys, ...extra]);
  for (const key of commonKeys) text(body[key], 36, UUID);
  if (tenantHeader !== body.tenant_id) invalid();
  const authority = Object.freeze({ tenantId: body.tenant_id as string, organizationId: body.organization_id as string,
    projectId: body.project_id as string, repoKey: body.repo_key as string });
  if (action === "stage") return service.stageEdit(stageRequest(body, authority));
  const common = { authority, editId: body.edit_id as string, actorBindingId: body.actor_binding_id as string,
    expectedVersion: version(body.expected_version), transitionKey: text(body.transition_key, 200) };
  if (action === "publish") return service.publishEdit({ ...common,
    confirmationId: text(body.confirmation_id, 36, UUID), receiptDigest: text(body.receipt_digest, 64, DIGEST) });
  return service.recoverEdit({ ...common, expectedMainCommit: text(body.expected_main_commit, 40, SHA),
    stagedHeadCommit: text(body.staged_head_commit, 40, SHA), stagedTree: text(body.staged_tree, 40, SHA),
    reasonCode: text(body.reason_code, 64, /^[a-z0-9_]+$/) });
}
