import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { createCodeBuildStaticNextExecutor } from "../src/projectBuild/codebuildStaticNextExecutor.js";
import type { CodeBuildStaticNextExecutorOptions, StaticNextBuildState } from "../src/projectBuild/codebuildStaticNextExecutor.js";
import { NEXT_PLAN, NextBuildError, nextRequestDigest } from "../src/projectBuild/genericNextBuild.js";
import type { NextBuildScope } from "../src/projectBuild/genericNextBuild.js";
import { ExecutorManagedNextRuntime } from "../src/projectBuild/managedNextRuntime.js";
import type { StaticNextJobInput } from "../src/projectBuild/managedNextRuntime.js";

const id = (n: number) => `00000000-0000-0000-0000-${String(n).padStart(12, "0")}`;
const hash = (value: Buffer | string) => createHash("sha256").update(value).digest("hex");
const scope: NextBuildScope = { tenantId: id(1), organizationId: id(2), projectId: id(3),
  repoKey: id(4), jobId: id(5), sourceCommit: "a".repeat(40), sourceTree: "b".repeat(40) };
function input(): StaticNextJobInput {
  const bundle = Buffer.from("synthetic immutable source bundle");
  return { protocol: "leaf.static-next.v1", executionId: nextRequestDigest(scope),
    bindingId: id(7), toolchainSha256: "c".repeat(64), bundleSha256: hash(bundle),
    scope: { ...scope }, bundle, plan: NEXT_PLAN };
}
const exported = [
  { path: "/index.html", body: Buffer.from("<main>export</main>").toString("base64"), contentType: "text/html" },
  { path: "/asset.js", body: Buffer.from("export default 1;").toString("base64"), contentType: "text/javascript" },
];
function output(value: StaticNextJobInput, changes: Record<string, unknown> = {}) {
  return { protocol: value.protocol, executionId: value.executionId, bindingId: value.bindingId,
    toolchainSha256: value.toolchainSha256, bundleSha256: value.bundleSha256,
    exitCode: 0, diagnostic: "private diagnostic", files: exported, ...changes };
}
function fixture() {
  const objects = new Map<string, Buffer>();
  let clock = 0;
  const store = {
    putIfAbsent: vi.fn(async (key: string, body: Buffer): Promise<"created" | "exists"> => {
      if (objects.has(key)) return "exists";
      objects.set(key, Buffer.from(body));
      return "created";
    }),
    get: vi.fn(async (key: string): Promise<Buffer | null> => {
      const bytes = objects.get(key);
      return bytes ? Buffer.from(bytes) : null;
    }),
  };
  const provider = {
    start: vi.fn(async (_executionId: string) => "build-new"),
    state: vi.fn(async (_buildId: string): Promise<StaticNextBuildState> => "SUCCEEDED"),
    stop: vi.fn(async (_buildId: string) => {}),
  };
  const sleep = vi.fn(async (ms: number) => { clock += ms; });
  const options: CodeBuildStaticNextExecutorOptions = { bindingId: id(7), toolchainSha256: "c".repeat(64),
    store, provider, pollMs: 100, deadlineMs: 250, now: () => clock, sleep };
  const executor = createCodeBuildStaticNextExecutor(options);
  const receipt = (value = input(), changes: Record<string, unknown> = {}) =>
    objects.set(`out/${value.executionId}.json`, Buffer.from(JSON.stringify(output(value, changes))));
  const mapping = (buildId = "build-existing", value = input()) =>
    objects.set(`map/${value.executionId}.json`, Buffer.from(JSON.stringify({ executionId: value.executionId, buildId })));
  return { objects, store, provider, sleep, options, executor, receipt, mapping };
}

describe("CodeBuild static Next executor over injected ports", () => {
  it("writes the exact wire serialization and decodes two files", async () => {
    const f = fixture();
    const value = input();
    f.receipt(value);
    const result = await f.executor.run(value);
    expect(f.executor.isolation).toBe("filesystem-and-network");
    expect(f.objects.get(`in/${value.executionId}.json`)).toEqual(Buffer.from(JSON.stringify({
      protocol: "leaf.static-next.v1", executionId: value.executionId, bindingId: id(7),
      toolchainSha256: "c".repeat(64), bundleSha256: hash(value.bundle),
      scope, bundle: value.bundle.toString("base64"), plan: NEXT_PLAN,
    })));
    expect(f.provider.start).toHaveBeenCalledExactlyOnceWith(value.executionId);
    expect(JSON.parse(f.objects.get(`map/${value.executionId}.json`)!.toString())).toEqual({
      executionId: value.executionId, buildId: "build-new",
    });
    expect(result).toEqual({ protocol: value.protocol, executionId: value.executionId,
      bindingId: value.bindingId, toolchainSha256: value.toolchainSha256,
      bundleSha256: value.bundleSha256, exitCode: 0,
      files: exported.map(file => ({ ...file, body: Buffer.from(file.body, "base64") })),
    });
    expect(result.files.every(file => Buffer.isBuffer(file.body))).toBe(true);
  });

  it("reuses a persisted build across executor instances and rejects different admitted bytes", async () => {
    const f = fixture();
    const value = input();
    f.receipt(value);
    const first = await f.executor.run(value);
    const second = createCodeBuildStaticNextExecutor(f.options);
    expect(await second.run(input())).toEqual(first);
    const changed = Buffer.from("different valid bundle");
    await expect(second.run({ ...value, bundle: changed, bundleSha256: hash(changed) }))
      .rejects.toMatchObject({ code: "job_conflict" });
    expect(f.provider.start).toHaveBeenCalledTimes(1);
  });

  it.each(["executionId", "bundleSha256", "oversize", "empty", "plan", "binding", "scope", "protocol", "toolchain"])(
    "rejects invalid %s before any port call", async variant => {
      const f = fixture();
      const value = input();
      let changed = value;
      if (variant === "executionId") changed = { ...value, executionId: "0".repeat(64) };
      if (variant === "bundleSha256") changed = { ...value, bundleSha256: "0".repeat(64) };
      if (variant === "oversize") changed = { ...value, bundle: Buffer.alloc(64 * 1024 * 1024 + 1) };
      if (variant === "empty") changed = { ...value, bundle: Buffer.alloc(0) };
      if (variant === "plan") changed = { ...value, plan: { ...NEXT_PLAN, network: "allow" } as unknown as typeof NEXT_PLAN };
      if (variant === "binding") changed = { ...value, bindingId: id(8) };
      if (variant === "scope") changed = { ...value, scope: { ...scope, projectId: "invalid" } };
      if (variant === "protocol") changed = { ...value, protocol: "foreign" as typeof value.protocol };
      if (variant === "toolchain") changed = { ...value, toolchainSha256: "d".repeat(64) };
      await expect(f.executor.run(changed)).rejects.toMatchObject({ code: "source_conflict" });
      for (const call of [f.store.get, f.store.putIfAbsent, f.provider.start, f.provider.state, f.provider.stop])
        expect(call).not.toHaveBeenCalled();
    });

  it("accepts a deep-equal plan independently of object identity and key order", async () => {
    const f = fixture();
    f.receipt();
    const plan: typeof NEXT_PLAN = { timeoutMs: NEXT_PLAN.timeoutMs, network: NEXT_PLAN.network,
      env: { NEXT_TELEMETRY_DISABLED: "1", NODE_ENV: "production" },
      output: NEXT_PLAN.output, build: [...NEXT_PLAN.build] };
    await expect(f.executor.run({ ...input(), plan })).resolves.toMatchObject({ exitCode: 0 });
  });

  it.each([10, 20, 30, 40, 50])("returns exit %s with no files or diagnostic", async exitCode => {
    const f = fixture();
    f.provider.state.mockResolvedValue("FAILED");
    f.receipt(input(), { exitCode, files: undefined, diagnostic: "secret diagnostic" });
    const result = await f.executor.run(input());
    expect(result.exitCode).toBe(exitCode);
    expect(result.files).toEqual([]);
    expect(result).not.toHaveProperty("diagnostic");
    expect(JSON.stringify(result)).not.toContain("secret diagnostic");
  });

  it.each(["protocol", "executionId", "bindingId", "toolchainSha256", "bundleSha256", "exitCode", "base64", "files", "json", "diagnostic"])(
    "rejects corrupt receipt %s without leaking its diagnostic", async variant => {
      const f = fixture();
      let changes: Record<string, unknown> = { [variant]: "foreign" };
      if (variant === "exitCode") changes = { exitCode: 99 };
      if (variant === "base64") changes = { files: [{ ...exported[0], body: "Zg" }] };
      if (variant === "files") changes = { files: null };
      if (variant === "diagnostic") changes = { diagnostic: "x".repeat(2049) };
      f.receipt(input(), changes);
      if (variant === "json") f.objects.set(`out/${input().executionId}.json`, Buffer.from("private diagnostic, invalid JSON"));
      await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError("producer_conflict"));
    });

  it.each(["FAILED", "FAULT", "TIMED_OUT", "STOPPED", "SUCCEEDED"] as const)(
    "fails closed for %s without an output", async state => {
      const f = fixture();
      f.provider.state.mockResolvedValue(state);
      await expect(f.executor.run(input())).rejects.toMatchObject({ code: "build_failed" });
    });

  it.each(["FAILED", "FAULT", "TIMED_OUT", "STOPPED"] as const)(
    "rejects a successful receipt when the provider reports %s", async state => {
      const f = fixture();
      f.provider.state.mockResolvedValue(state);
      f.receipt();
      await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError("producer_conflict"));
      expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-new");
    });

  it.each(["count", "encoded size", "relative", "empty segment", "trailing slash", "dot", "parent",
    "invalid character", "duplicate", "long path"])("rejects receipt files with %s before decoding any body", async variant => {
      const f = fixture();
      let files = [...exported];
      if (variant === "count") files = Array.from({ length: 4097 }, (_, i) => ({ ...exported[0], path: `/file-${i}` }));
      if (variant === "encoded size") {
        const body = "A".repeat(4 * Math.ceil(32 * 1024 * 1024 / 3 / 2));
        files = exported.map(file => ({ ...file, body }));
      }
      const paths: Record<string, string> = { relative: "relative.html", "empty segment": "/a//b",
        "trailing slash": "/a/", dot: "/a/./b", parent: "/a/../b", "invalid character": "/a b",
        duplicate: exported[0].path, "long path": `/${"a".repeat(2048)}` };
      if (paths[variant]) files[1] = { ...files[1], path: paths[variant] };
      f.receipt(input(), { files });
      const from = vi.spyOn(Buffer, "from");
      try {
        await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError("producer_conflict"));
        expect(from.mock.calls.filter(call => call[1] === "base64")).toHaveLength(0);
      } finally { from.mockRestore(); }
    });

  it.each(["state error", "unknown state", "output read", "receipt parse"])(
    "stops the build after a poll failure: %s", async variant => {
      const f = fixture();
      if (variant === "state error") f.provider.state.mockRejectedValue(new Error("private state error"));
      if (variant === "unknown state") f.provider.state.mockResolvedValue("UNKNOWN" as StaticNextBuildState);
      if (variant === "output read") {
        const get = f.store.get.getMockImplementation()!;
        f.store.get.mockImplementation(async key => {
          if (key.startsWith("out/")) throw new Error("private store error");
          return get(key);
        });
      }
      if (variant === "receipt parse") f.objects.set(`out/${input().executionId}.json`, Buffer.from("invalid JSON"));
      // Cleanup errors must not replace the original failure or leak provider details.
      f.provider.stop.mockRejectedValue(new Error("private stop error"));
      await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError(
        variant === "receipt parse" ? "producer_conflict" : "build_failed"));
      expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-new");
    });

  it("polls queued and running states before consuming a receipt", async () => {
    const f = fixture();
    f.receipt();
    f.provider.state.mockResolvedValueOnce("QUEUED").mockResolvedValueOnce("IN_PROGRESS");
    await expect(f.executor.run(input())).resolves.toMatchObject({ exitCode: 0 });
    expect(f.sleep.mock.calls).toEqual([[100], [100]]);
    expect(f.provider.state).toHaveBeenCalledTimes(3);
  });

  it("stops at the deadline and reports build_failed", async () => {
    const f = fixture();
    f.provider.state.mockResolvedValue("IN_PROGRESS");
    await expect(f.executor.run(input())).rejects.toMatchObject({ code: "build_failed" });
    expect(f.sleep.mock.calls).toEqual([[100], [100], [50]]);
    expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-new");
  });

  it("stops a losing submission and uses the winning mapping", async () => {
    const f = fixture();
    f.receipt();
    const put = f.store.putIfAbsent.getMockImplementation()!;
    f.store.putIfAbsent.mockImplementation(async (key, bytes) => {
      if (key.startsWith("map/")) { f.mapping("build-winner"); return "exists"; }
      return put(key, bytes);
    });
    await expect(f.executor.run(input())).resolves.toMatchObject({ exitCode: 0 });
    expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-new");
    expect(f.provider.state).toHaveBeenCalledExactlyOnceWith("build-winner");
  });

  it.each(["put throws", "get throws", "missing", "malformed", "foreign", "unusable put"])(
    "stops the started build when persisting its mapping fails: %s", async variant => {
      const f = fixture();
      const put = f.store.putIfAbsent.getMockImplementation()!;
      f.store.putIfAbsent.mockImplementation(async (key, bytes) => {
        if (!key.startsWith("map/")) return put(key, bytes);
        if (variant === "put throws") throw new Error("private mapping error");
        if (variant === "unusable put") return "invalid" as "exists";
        if (variant === "malformed") f.objects.set(key, Buffer.from("not JSON"));
        if (variant === "foreign") f.objects.set(key, Buffer.from(JSON.stringify({ executionId: "foreign", buildId: "other" })));
        return "exists";
      });
      const get = f.store.get.getMockImplementation()!;
      let mapReads = 0;
      f.store.get.mockImplementation(async key => {
        if (key.startsWith("map/") && ++mapReads > 1 && variant === "get throws")
          throw new Error("private mapping read error");
        return get(key);
      });
      f.provider.stop.mockRejectedValue(new Error("private stop error"));
      await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError(
        variant === "put throws" || variant === "get throws" ? "build_failed" : "job_conflict"));
      expect(f.provider.start).toHaveBeenCalledExactlyOnceWith(input().executionId);
      expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-new");
      expect(f.provider.state).not.toHaveBeenCalled();
    });

  it("rejects malformed and foreign mappings without starting a build", async () => {
    for (const bytes of ["not json", JSON.stringify({ executionId: "foreign", buildId: "other" }),
      JSON.stringify({ executionId: input().executionId, buildId: "" })]) {
      const f = fixture();
      f.objects.set(`map/${input().executionId}.json`, Buffer.from(bytes));
      await expect(f.executor.run(input())).rejects.toMatchObject({ code: "job_conflict" });
      expect(f.provider.start).not.toHaveBeenCalled();
    }
  });

  it("does not expose arbitrary provider error messages", async () => {
    const f = fixture();
    f.provider.start.mockRejectedValue(new Error("private provider details"));
    await expect(f.executor.run(input())).rejects.toEqual(new NextBuildError("build_failed"));
    await expect(f.executor.stop(input())).resolves.toMatchObject({ confirmed: false });
  });

  it("confirms no admission but leaves an uncertain submission unconfirmed", async () => {
    const f = fixture();
    await expect(f.executor.stop(input())).resolves.toEqual({ protocol: input().protocol,
      executionId: input().executionId, bindingId: id(7), confirmed: true });
    f.objects.set(`in/${input().executionId}.json`, Buffer.from("admitted"));
    await expect(f.executor.stop(input())).resolves.toMatchObject({ confirmed: false });
    expect(f.provider.stop).not.toHaveBeenCalled();
    expect(f.provider.state).not.toHaveBeenCalled();
  });

  it("stops running builds and confirms only an observed terminal state", async () => {
    const f = fixture();
    f.mapping();
    f.provider.state.mockResolvedValueOnce("IN_PROGRESS").mockResolvedValueOnce("IN_PROGRESS").mockResolvedValue("STOPPED");
    await expect(f.executor.stop(input())).resolves.toMatchObject({ confirmed: true });
    expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-existing");
    expect(f.sleep).toHaveBeenCalledExactlyOnceWith(100);
  });

  it("leaves cleanup unconfirmed at its deadline", async () => {
    const f = fixture();
    f.mapping();
    f.provider.state.mockResolvedValue("QUEUED");
    await expect(f.executor.stop(input())).resolves.toMatchObject({ confirmed: false });
    expect(f.provider.stop).toHaveBeenCalledExactlyOnceWith("build-existing");
  });

  it("does not stop an already terminal build", async () => {
    const f = fixture();
    f.mapping();
    await expect(f.executor.stop(input())).resolves.toMatchObject({ confirmed: true });
    expect(f.provider.stop).not.toHaveBeenCalled();
  });

  it.each(["protocol", "bindingId", "executionId"])("rejects invalid cleanup %s before port calls", async field => {
    const f = fixture();
    await expect(f.executor.stop({ ...input(), [field]: "invalid" })).rejects.toMatchObject({ code: "cleanup_required" });
    expect(f.store.get).not.toHaveBeenCalled();
    expect(f.provider.stop).not.toHaveBeenCalled();
  });

  it.each([
    { bindingId: "bad" }, { toolchainSha256: "F".repeat(64) },
    { store: {} }, { provider: {} }, { store: { get() {} } }, { provider: { start() {}, state() {} } },
    { pollMs: 99 }, { pollMs: 60001 }, { pollMs: NaN }, { deadlineMs: 0 },
    { deadlineMs: 3600001 }, { deadlineMs: Infinity }, { now: 1 }, { sleep: 1 },
  ])("rejects invalid factory options %j", changes => {
    const f = fixture();
    expect(() => createCodeBuildStaticNextExecutor({ ...f.options, ...changes } as CodeBuildStaticNextExecutorOptions))
      .toThrow("isolated_next_binding_unavailable");
  });

  it("composes with the runtime for an unsubmitted lease and cleanup", async () => {
    const f = fixture();
    const runtime = new ExecutorManagedNextRuntime(f.executor);
    const lease = await runtime.create(scope);
    await expect(lease.destroy()).resolves.toEqual({ confirmed: true });
    expect(f.provider.start).not.toHaveBeenCalled();
  });

  it("rejects the obsolete object digest before admitting input", async () => {
    const f = fixture();
    await expect(f.executor.run({ ...input(), executionId: hash(JSON.stringify(scope)) }))
      .rejects.toMatchObject({ code: "source_conflict" });
    expect(f.store.get).not.toHaveBeenCalled();
    expect(f.store.putIfAbsent).not.toHaveBeenCalled();
    expect(f.provider.start).not.toHaveBeenCalled();
    expect(f.provider.state).not.toHaveBeenCalled();
    expect(f.provider.stop).not.toHaveBeenCalled();
  });

  it("composes with the runtime for a successful build and confirmed cleanup", async () => {
    const f = fixture();
    const value = input();
    f.receipt(value);
    const lease = await new ExecutorManagedNextRuntime(f.executor).create(scope);
    const result = await lease.build(value.bundle, value.bundleSha256, NEXT_PLAN);
    expect(result).toEqual({ requestDigest: nextRequestDigest(scope), bundleSha256: value.bundleSha256,
      files: [...exported].sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
        .map(file => ({ ...file, body: Buffer.from(file.body, "base64") })),
    });
    expect(f.provider.start).toHaveBeenCalledExactlyOnceWith(nextRequestDigest(scope));
    await expect(lease.destroy()).resolves.toEqual({ confirmed: true });
    expect(f.provider.stop).not.toHaveBeenCalled();
  });
});
