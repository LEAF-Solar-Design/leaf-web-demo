import { createHash } from "node:crypto";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { afterEach, describe, expect, it } from "vitest";
import { NEXT_PLAN, nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { NextBuildScope, StaticFile } from "../src/projectBuild/genericNextBuild.js";
import { ExecutorManagedNextRuntime } from "../src/projectBuild/managedNextRuntime.js";
import type { StaticNextJobExecutor, StaticNextJobIdentity, StaticNextJobInput, StaticNextJobResult } from "../src/projectBuild/managedNextRuntime.js";
import { createManagedStaticNextProducer } from "../src/projectBuild/managedStaticNextComposition.js";
import { DiskPrivateStaticArtifacts } from "../src/projectBuild/privateStaticArtifactStore.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const authority = { tenantId: id(1), organizationId: id(2), projectId: id(3), repoKey: id(4) };
const request = { jobId: id(5), sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
const scope: NextBuildScope = { ...authority, ...request };
const hash = (v: Buffer | string) => createHash("sha256").update(v).digest("hex");
const bundle = Buffer.from("synthetic immutable bundle; no tenant source execution");
const digest = hash(bundle);
const files = (): StaticFile[] => [{ path: "/index.html", body: Buffer.from("<main>static export</main>"), contentType: "text/html" }];
const directories: string[] = [];
afterEach(async () => { for (const path of directories.splice(0)) await rm(path, { recursive: true, force: true }); });
async function temp() {
  const path = await mkdtemp(join(tmpdir(), "b03-runtime-"));
  directories.push(path);
  return path;
}
function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
/** Stateful transport fixture, not isolation evidence. Actual disk store and
 * producer are used by composition tests below. */
function executorFixture() {
  const runs: StaticNextJobInput[] = [];
  const stops: StaticNextJobIdentity[] = [];
  const response = (input: StaticNextJobInput): StaticNextJobResult => ({ protocol: input.protocol,
    executionId: input.executionId, bindingId: input.bindingId, toolchainSha256: input.toolchainSha256,
    bundleSha256: input.bundleSha256, exitCode: 0, files: files() });
  const executor: StaticNextJobExecutor = { bindingId: id(7), toolchainSha256: "c".repeat(64),
    isolation: "filesystem-and-network",
    async run(input) { runs.push(input); return response(input); },
    async stop(identity) { stops.push(identity); return { protocol: identity.protocol,
      executionId: identity.executionId, bindingId: identity.bindingId, confirmed: true }; },
  };
  return { executor, response, runs, stops };
}

describe("managed static Next executor custody and cleanup", () => {
  it("binds exact source, bundle, plan and supply and submits only once", async () => {
    const f = executorFixture();
    const runtime = new ExecutorManagedNextRuntime(f.executor);
    const mutable = { ...scope };
    const lease = await runtime.create(mutable);
    mutable.sourceCommit = "e".repeat(40);
    const bytes = Buffer.from(bundle);
    const one = lease.build(bytes, digest, NEXT_PLAN);
    bytes.fill(0);
    const two = lease.build(bundle, digest, NEXT_PLAN);
    expect(one).toBe(two);
    expect(await one).toMatchObject({ requestDigest: nextRequestDigest(scope), bundleSha256: digest });
    expect(f.runs).toHaveLength(1);
    expect(f.runs[0]).toMatchObject({ scope, executionId: nextRequestDigest(scope), bundle,
      toolchainSha256: runtime.toolchainSha256, plan: NEXT_PLAN });
    await expect(lease.build(Buffer.from("changed"), hash("changed"), NEXT_PLAN)).rejects.toMatchObject({ code: "job_conflict" });
    expect(await lease.destroy()).toEqual({ confirmed: true });
    await lease.destroy();
    expect(f.stops).toHaveLength(1);
    await expect(lease.build(bundle, digest, NEXT_PLAN)).rejects.toMatchObject({ code: "preview_released" });
  });

  it("rejects malformed supply and authority before dispatch", async () => {
    const f = executorFixture();
    expect(() => new ExecutorManagedNextRuntime({ ...f.executor, toolchainSha256: "tag:latest" })).toThrow("isolated_next_binding_unavailable");
    await expect(new ExecutorManagedNextRuntime(f.executor).create({ ...scope, projectId: "other" })).rejects.toMatchObject({ code: "source_conflict" });
    expect(f.runs).toHaveLength(0);
  });

  it("rejects changed command policy and corrupt bundles without dispatch", async () => {
    const f = executorFixture();
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    await expect(lease.build(bundle, digest, { ...NEXT_PLAN })).rejects.toMatchObject({ code: "source_conflict" });
    await expect(lease.build(bundle, "f".repeat(64), NEXT_PLAN)).rejects.toMatchObject({ code: "source_conflict" });
    expect(f.runs).toHaveLength(0);
    await lease.destroy();
  });

  it.each(["protocol", "executionId", "bindingId", "toolchainSha256", "bundleSha256"] as const)("rejects foreign result %s", async field => {
    const f = executorFixture();
    f.executor.run = async input => ({ ...f.response(input), [field]: "foreign" });
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    await expect(lease.build(bundle, digest, NEXT_PLAN)).rejects.toMatchObject({ code: "producer_conflict" });
    await lease.destroy();
  });

  it.each([[10, "build_failed"], [20, "build_timeout"], [30, "runtime_policy_failed"],
    [40, "source_conflict"], [50, "toolchain_unavailable"], [99, "producer_conflict"]])("maps exit %s without accepting files", async (exitCode, code) => {
    const f = executorFixture();
    f.executor.run = async input => ({ ...f.response(input), exitCode: Number(exitCode) });
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    await expect(lease.build(bundle, digest, NEXT_PLAN)).rejects.toMatchObject({ code });
    await lease.destroy();
  });

  it.each(["traversal", "duplicate", "missing-index", "oversize", "type"])("rejects invalid exported files: %s", async variant => {
    const f = executorFixture();
    f.executor.run = async input => {
      let output = files();
      if (variant === "traversal") output = [{ ...output[0], path: "/../index.html" }];
      if (variant === "duplicate") output.push(output[0]);
      if (variant === "missing-index") output = [{ ...output[0], path: "/other.html" }];
      if (variant === "oversize") output = [{ ...output[0], body: Buffer.alloc(2 * 1024 * 1024 + 1) }];
      if (variant === "type") output = [{ ...output[0], contentType: "text/html\r\nX: injected" }];
      return { ...f.response(input), files: output };
    };
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    await expect(lease.build(bundle, digest, NEXT_PLAN)).rejects.toMatchObject({ code: "producer_conflict" });
    await lease.destroy();
  });

  it("does not resubmit an uncertain run; stops by the original identity", async () => {
    const f = executorFixture();
    f.executor.run = async input => { f.runs.push(input); throw new Error("lost acknowledgement"); };
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    const build = lease.build(bundle, digest, NEXT_PLAN);
    await expect(build).rejects.toMatchObject({ code: "build_failed" });
    expect(lease.build(bundle, digest, NEXT_PLAN)).toBe(build);
    await lease.destroy();
    expect(f.runs).toHaveLength(1);
    expect(f.stops[0].executionId).toBe(nextRequestDigest(scope));
  });

  it("fences results during concurrent cancellation without waiting on build", async () => {
    const f = executorFixture();
    const gate = deferred<StaticNextJobResult>();
    f.executor.run = async input => { f.runs.push(input); return gate.promise; };
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    const build = lease.build(bundle, digest, NEXT_PLAN);
    const rejected = expect(build).rejects.toMatchObject({ code: "preview_released" });
    await expect(lease.destroy()).resolves.toEqual({ confirmed: true });
    gate.resolve(f.response(f.runs[0]));
    await rejected;
  });

  it("coalesces cleanup and retries only unconfirmed cleanup", async () => {
    const f = executorFixture();
    const gate = deferred<Awaited<ReturnType<StaticNextJobExecutor["stop"]>>>();
    let count = 0;
    f.executor.stop = async () => { count++; return gate.promise; };
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    const a = lease.destroy();
    const b = lease.destroy();
    expect(a).toBe(b);
    gate.resolve({ protocol: "leaf.static-next.v1", executionId: "foreign", bindingId: id(7), confirmed: true });
    await expect(a).rejects.toMatchObject({ code: "cleanup_required" });
    await expect(lease.destroy()).rejects.toMatchObject({ code: "cleanup_required" });
    expect(count).toBe(2);
  });

  it("composes actual durable storage and withholds ready until cleanup, then enforces revocation", async () => {
    const f = executorFixture();
    let current = true;
    const stop = deferred<Awaited<ReturnType<StaticNextJobExecutor["stop"]>>>();
    const stopping = deferred<StaticNextJobIdentity>();
    f.executor.stop = async identity => { stopping.resolve(identity); return stop.promise; };
    const root = join(await temp(), "artifacts");
    const producer = createManagedStaticNextProducer({ executor: f.executor, artifactRoot: root,
      artifactBindingId: id(8), isCurrent: async () => current,
      repository: { async exportProjectSourceBundle() { return { ...request, bundle,
        bundleSha256: digest, sizeBytes: bundle.length, leaseId: id(9), leaseGeneration: "1" }; } } });
    const building = producer.build(authority, request);
    const identity = await stopping.promise;
    expect(producer.status(authority, request.jobId)).toEqual({ ready: false, cleanup: "pending" });
    await expect(producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "preview_unavailable" });
    stop.resolve({ protocol: identity.protocol, executionId: identity.executionId,
      bindingId: identity.bindingId, confirmed: true });
    const preview = await building;
    expect((await producer.read(authority, request.jobId)).body).toEqual(files()[0].body);
    const reopened = new DiskPrivateStaticArtifacts({ root, bindingId: id(8), isCurrent: async () => current });
    expect((await reopened.read(scope, preview.artifactId, "/index.html", 1024)).body).toEqual(files()[0].body);
    current = false;
    await expect(producer.read(authority, request.jobId)).rejects.toMatchObject({ code: "source_stale" });
    await expect(reopened.read(scope, preview.artifactId, "/index.html", 1024)).rejects.toMatchObject({ code: "source_stale" });
  });
});

// This trusted fixed fixture may execute locally; it is NOT a production
// executor, Git materializer or isolation attestation. Never use tenant input.
const webRequire = createRequire(new URL("../../web/package.json", import.meta.url));
let nextCli: string | undefined;
try { nextCli = webRequire.resolve("next/dist/bin/next"); } catch { /* supply absent */ }
it.skipIf(!nextCli)("exports a real tiny Next app through composition and disk custody (local fixture, not isolation)", async () => {
  const directory = await temp();
  const source = join(directory, "source");
  const project = join(directory, "app");
  await mkdir(join(source, "pages"), { recursive: true });
  await writeFile(join(source, "package.json"), JSON.stringify({ private: true }));
  await writeFile(join(source, "next.config.mjs"), "export default {output:'export',experimental:{cpus:1}};\n");
  await writeFile(join(source, "pages/index.js"), "export default function Page(){return <main>B03 real static export</main>}\n");
  const execute = promisify(execFile);
  const git = async (args: string[], cwd = source) => (await execute("git", args,
    { cwd, env: { GIT_CONFIG_NOSYSTEM: "1" }, timeout: 20_000 })).stdout.trim();
  await git(["init", "--quiet"]);
  await git(["add", "."]);
  await git(["-c", "user.name=B03 fixture", "-c", "user.email=b03@local.invalid", "commit", "--quiet", "-m", "fixed test app"]);
  const actualRequest = { jobId: request.jobId, sourceCommit: await git(["rev-parse", "HEAD"]),
    sourceTree: await git(["rev-parse", "HEAD^{tree}"]) };
  const archive = join(directory, "source.bundle");
  await git(["bundle", "create", archive, "--all"]);
  const actualBundle = await readFile(archive);
  const actualDigest = hash(actualBundle);
  const f = executorFixture();
  const supply = JSON.stringify({ node: process.version, next: webRequire("next/package.json").version,
    cliSha256: hash(await readFile(nextCli!)) });
  Object.assign(f.executor, { toolchainSha256: hash(supply) });
  f.executor.run = async input => {
    expect(hash(input.bundle)).toBe(input.bundleSha256);
    await writeFile(join(directory, "input.bundle"), input.bundle);
    await git(["clone", "--no-checkout", join(directory, "input.bundle"), project], directory);
    await git(["checkout", "--detach", input.scope.sourceCommit], project);
    expect(await git(["rev-parse", "HEAD^{tree}"], project)).toBe(input.scope.sourceTree);
    await symlink(dirname(dirname(dirname(dirname(nextCli!)))), join(project, "node_modules"), "junction");
    // No inherited credentials or environment values. A clean environment is
    // sufficient for this known Next fixture, never an isolation mechanism.
    await execute(process.execPath, [nextCli!, "build", "--webpack"], { cwd: project,
      env: { NODE_ENV: "production", NEXT_TELEMETRY_DISABLED: "1" }, timeout: 120_000, maxBuffer: 1024 * 1024 });
    const output: StaticFile[] = [];
    async function collect(base: string, prefix = "") {
      for (const entry of await readdir(base, { withFileTypes: true })) {
        const relative = `${prefix}/${entry.name}`;
        if (entry.isDirectory()) await collect(join(base, entry.name), relative);
        else if (entry.isFile()) output.push({ path: relative, body: await readFile(join(base, entry.name)),
          contentType: relative.endsWith(".html") ? "text/html" : relative.endsWith(".js") ? "text/javascript" : "application/octet-stream" });
        else throw new Error("unexpected output link");
      }
    }
    await collect(join(project, "out"));
    return { ...f.response(input), files: output };
  };
  f.executor.stop = async identity => {
    await rm(project, { recursive: true, force: true });
    return { protocol: identity.protocol, executionId: identity.executionId, bindingId: identity.bindingId, confirmed: true };
  };
  const producer = createManagedStaticNextProducer({ executor: f.executor, artifactRoot: join(directory, "private"),
    artifactBindingId: id(8), isCurrent: async () => true,
    repository: { async exportProjectSourceBundle() { return { ...actualRequest, bundle: actualBundle,
      bundleSha256: actualDigest, sizeBytes: actualBundle.length, leaseId: id(9), leaseGeneration: "1" }; } } });
  const preview = await producer.build(authority, actualRequest);
  expect((await producer.read(authority, request.jobId)).body.toString()).toContain("B03 real static export");
  expect(preview.manifest.files.some(file => file.path.endsWith(".js"))).toBe(true);
  expect(producer.status(authority, request.jobId)).toEqual({ ready: true, cleanup: "confirmed" });
}, 150_000);
