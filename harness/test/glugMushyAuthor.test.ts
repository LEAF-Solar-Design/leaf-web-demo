import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  GLUG_MUSHY_SOURCE_COMMIT,
  GlugMushyAuthorError,
  PinnedGlugMushyAuthor,
  verifyPinnedMushyArtifact,
} from "../src/glugMushyAuthor.js";
import type { AgentGrant, OAuthGrantProvider } from "../src/ports/index.js";

const CLAIM_ID = "claim-123";
const BASE_PROMPT = "Update the Glug weekend welcome copy";

function sha256(value: string | Buffer): string {
  return createHash("sha256").update(value).digest("hex");
}

function git(repo: string, ...args: string[]): string {
  return execFileSync("git", ["-C", repo, ...args], { encoding: "utf8" }).trim();
}

function fixture(): {
  root: string;
  artifact: string;
  manifest: string;
  workspaces: string;
  repo: string;
  base: string;
} {
  const root = mkdtempSync(join(tmpdir(), "glug-mushy-author-"));
  const artifact = join(root, "artifact");
  const workspaces = join(root, "workspaces");
  const repo = join(workspaces, sha256(CLAIM_ID), "repository");
  mkdirSync(join(artifact, "src", "ports", "impl"), { recursive: true });
  mkdirSync(repo, { recursive: true });
  const artifactPayloads = new Map<string, string>([
    ["src/index.js", "export const pinned = true;\n"],
    ["src/ports/impl/repoEditRunner.js", "export class SdkRepoEditor {}\n"],
  ]);
  for (const [path, payload] of artifactPayloads) {
    const target = join(artifact, path);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, payload);
  }
  const files = [...artifactPayloads.entries()].sort(([a], [b]) => a.localeCompare(b)).map(
    ([path, payload]) => ({ bytes: Buffer.byteLength(payload), path, sha256: sha256(payload) }),
  );
  const manifest = join(root, "manifest.json");
  writeFileSync(manifest, JSON.stringify({
    workspace_id: "glug",
    sources: { mushy_source_commit: GLUG_MUSHY_SOURCE_COMMIT },
    artifact: {
      entrypoint: "src/index.js",
      files,
      byte_count: files.reduce((total, file) => total + file.bytes, 0),
      aggregate_sha256: sha256(JSON.stringify(files)),
    },
    limits: { author_timeout_seconds: 240 },
  }));

  execFileSync("git", ["init", "-b", "main", repo]);
  writeFileSync(join(repo, ".gitignore"), ".env\n");
  writeFileSync(join(repo, "welcome.txt"), "Original\n");
  git(repo, "add", ".gitignore", "welcome.txt");
  execFileSync("git", [
    "-C", repo,
    "-c", "user.name=Fixture",
    "-c", "user.email=fixture@example.invalid",
    "commit", "-m", "fixture",
  ]);
  return { root, artifact, manifest, workspaces, repo, base: git(repo, "rev-parse", "HEAD") };
}

const grantProvider: OAuthGrantProvider = {
  async getGrant(): Promise<AgentGrant> {
    return { kind: "oauth", oauthToken: "test-only-oauth-grant-value" };
  },
};

function request(base: string): Record<string, unknown> {
  return {
    contract: "glug.mushy-author-request.v1",
    workspace: "glug",
    power: "stage_change",
    instruction: BASE_PROMPT,
    base_commit: base,
    claim_id: CLAIM_ID,
  };
}

describe("PinnedGlugMushyAuthor", () => {
  const roots: string[] = [];
  afterEach(() => {
    vi.useRealTimers();
    for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
  });

  it("verifies the exact manifest-declared artifact tree", () => {
    const f = fixture();
    roots.push(f.root);
    expect(
      verifyPinnedMushyArtifact(f.artifact, f.manifest, GLUG_MUSHY_SOURCE_COMMIT).workspace_id,
    ).toBe("glug");
    writeFileSync(join(f.artifact, "src", "index.js"), "export const pinned = false;\n");
    expect(() => verifyPinnedMushyArtifact(
      f.artifact,
      f.manifest,
      GLUG_MUSHY_SOURCE_COMMIT,
    )).toThrowError(GlugMushyAuthorError);
  });

  it("restores tracked, untracked, and ignored writes after a read-only answer", async () => {
    const f = fixture();
    roots.push(f.root);
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider,
      editorFactory: () => ({
        async edit({ repoDir }) {
          writeFileSync(join(repoDir, "welcome.txt"), "Changed\n");
          writeFileSync(join(repoDir, "scratch.txt"), "temporary\n");
          writeFileSync(join(repoDir, ".env"), "temporary=true\n");
          return { summary: "Draft welcome copy" };
        },
      }),
    });
    const result = await author.run(
      { ...request(f.base), power: "announcement_draft" },
      GLUG_MUSHY_SOURCE_COMMIT,
      240,
    );
    expect(result).toEqual({ text: "Draft welcome copy" });
    expect(readFileSync(join(f.repo, "welcome.txt"), "utf8").replaceAll("\r\n", "\n")).toBe("Original\n");
    expect(git(f.repo, "status", "--porcelain=v1", "--ignored")).toBe("");
    expect(git(f.repo, "rev-parse", "HEAD")).toBe(f.base);
  });

  it("commits a staged change with fixed provenance and never puts the prompt in Git", async () => {
    const f = fixture();
    roots.push(f.root);
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider,
      editorFactory: () => ({
        async edit({ repoDir }) {
          writeFileSync(join(repoDir, "welcome.txt"), "New Glug welcome\n");
          return { summary: "Updated welcome copy" };
        },
      }),
    });
    expect(await author.run(request(f.base), GLUG_MUSHY_SOURCE_COMMIT, 240)).toEqual({});
    expect(git(f.repo, "rev-parse", "HEAD")).not.toBe(f.base);
    const message = git(f.repo, "log", "-1", "--format=%B");
    expect(message).toContain(`Mushy-Source: ${GLUG_MUSHY_SOURCE_COMMIT}`);
    expect(message).not.toContain(BASE_PROMPT);
    expect(git(f.repo, "status", "--porcelain=v1", "--ignored")).toBe("");
  });

  it("rejects extra request fields before invoking the editor", async () => {
    const f = fixture();
    roots.push(f.root);
    let invoked = false;
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider,
      editorFactory: () => ({
        async edit() {
          invoked = true;
          return { summary: "unexpected" };
        },
      }),
    });
    await expect(author.run(
      { ...request(f.base), repository: f.repo },
      GLUG_MUSHY_SOURCE_COMMIT,
      240,
    )).rejects.toMatchObject({ code: "request_invalid" });
    expect(invoked).toBe(false);
  });

  it("rejects an editor that changes Git authority before the trusted commit", async () => {
    const f = fixture();
    roots.push(f.root);
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider,
      editorFactory: () => ({
        async edit({ repoDir }) {
          writeFileSync(join(repoDir, "welcome.txt"), "Untrusted commit\n");
          git(repoDir, "add", "welcome.txt");
          execFileSync("git", [
            "-C", repoDir,
            "-c", "user.name=Untrusted",
            "-c", "user.email=untrusted@example.invalid",
            "commit", "-m", "untrusted",
          ]);
          return { summary: "unexpected" };
        },
      }),
    });
    await expect(author.run(
      request(f.base),
      GLUG_MUSHY_SOURCE_COMMIT,
      240,
    )).rejects.toMatchObject({ code: "workspace_invalid" });
  });

  it("deducts grant setup time from the editor budget and passes the outer abort signal", async () => {
    vi.useFakeTimers({ now: new Date("2030-01-01T00:00:00.000Z") });
    const f = fixture();
    roots.push(f.root);
    let editorBudget = 0;
    let receivedSignal: AbortSignal | undefined;
    const delayedGrantProvider: OAuthGrantProvider = {
      async getGrant(): Promise<AgentGrant> {
        await new Promise<void>((resolve) => setTimeout(resolve, 50_000));
        return { kind: "oauth", oauthToken: "test-only-oauth-grant-value" };
      },
    };
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider: delayedGrantProvider,
      editorFactory: (maxWallTimeMs) => {
        editorBudget = maxWallTimeMs;
        return {
          async edit({ repoDir, signal }) {
            receivedSignal = signal;
            writeFileSync(join(repoDir, "welcome.txt"), "Deadline-bound change\n");
            return { summary: "Updated welcome copy" };
          },
        };
      },
    });

    const run = author.run(request(f.base), GLUG_MUSHY_SOURCE_COMMIT, 240);
    await vi.advanceTimersByTimeAsync(50_000);
    await expect(run).resolves.toEqual({});

    expect(editorBudget).toBeGreaterThan(0);
    expect(editorBudget).toBeLessThanOrEqual(190_000);
    expect(receivedSignal).toBeInstanceOf(AbortSignal);
    expect(receivedSignal?.aborted).toBe(false);
  });

  it("ends a stalled setup at the one end-to-end author deadline", async () => {
    vi.useFakeTimers({ now: new Date("2030-01-01T00:00:00.000Z") });
    const f = fixture();
    roots.push(f.root);
    let editorInvoked = false;
    const stalledGrantProvider: OAuthGrantProvider = {
      async getGrant(): Promise<AgentGrant> {
        return new Promise<AgentGrant>(() => undefined);
      },
    };
    const author = new PinnedGlugMushyAuthor({
      artifactRoot: f.artifact,
      manifestPath: f.manifest,
      workspaceRoot: f.workspaces,
      sourceCommit: GLUG_MUSHY_SOURCE_COMMIT,
      grantTenantId: "glug-maintainer",
      grantProvider: stalledGrantProvider,
      editorFactory: () => ({
        async edit() {
          editorInvoked = true;
          return { summary: "unexpected" };
        },
      }),
    });

    const run = author.run(request(f.base), GLUG_MUSHY_SOURCE_COMMIT, 240);
    const rejection = expect(run).rejects.toMatchObject({ code: "author_timeout", status: 504 });
    await vi.advanceTimersByTimeAsync(240_000);
    await rejection;
    expect(editorInvoked).toBe(false);
  });
});
