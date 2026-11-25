import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { GetObjectCommand } from "@aws-sdk/client-s3";
import { describe, expect, it, vi } from "vitest";
import { createHostStaticNextExecutor } from "../src/projectBuild/staticNextHostWiring.js";

const config = () => ({
  LEAF_STATIC_NEXT_EXECUTOR: "enabled",
  LEAF_STATIC_NEXT_BUCKET: "private-bucket",
  LEAF_STATIC_NEXT_PROJECT: "static-next",
  LEAF_STATIC_NEXT_TOOLCHAIN_SHA256: "a".repeat(64),
  LEAF_STATIC_NEXT_BINDING_ID: "00000000-0000-0000-0000-000000000001",
  LEAF_STATIC_NEXT_ARTIFACT_ROOT: resolve(tmpdir(), "..", "static-next-private-artifacts"),
  AWS_REGION: "us-east-1",
});
const clients = () => ({ s3Client: { send: vi.fn() }, codeBuildClient: { send: vi.fn() } });

describe("opt-in static Next host wiring", () => {
  it.each([undefined, "disabled", "true", "ENABLED", ""])("is disabled for %s", flag => {
    expect(createHostStaticNextExecutor({ LEAF_STATIC_NEXT_EXECUTOR: flag })).toBeNull();
  });

  it.each(["LEAF_STATIC_NEXT_BUCKET", "LEAF_STATIC_NEXT_PROJECT", "LEAF_STATIC_NEXT_TOOLCHAIN_SHA256",
    "LEAF_STATIC_NEXT_BINDING_ID", "LEAF_STATIC_NEXT_ARTIFACT_ROOT"] as const)("requires %s", name => {
      for (const value of [undefined, "", " "]) {
        const deps = clients();
        expect(() => createHostStaticNextExecutor({ ...config(), [name]: value }, deps))
          .toThrow(`static_next_config_invalid: ${name}`);
        expect(deps.s3Client.send).not.toHaveBeenCalled();
        expect(deps.codeBuildClient.send).not.toHaveBeenCalled();
      }
    });

  it.each(["a".repeat(63), "A".repeat(64), "z".repeat(64)])("refuses invalid toolchain hash %s", hash => {
    expect(() => createHostStaticNextExecutor({ ...config(), LEAF_STATIC_NEXT_TOOLCHAIN_SHA256: hash }, clients()))
      .toThrow("static_next_config_invalid: LEAF_STATIC_NEXT_TOOLCHAIN_SHA256");
  });

  it("names an invalid binding ID", () => {
    expect(() => createHostStaticNextExecutor({ ...config(), LEAF_STATIC_NEXT_BINDING_ID: "bad" }, clients()))
      .toThrow("static_next_config_invalid: LEAF_STATIC_NEXT_BINDING_ID");
  });

  it.each(["relative/path", tmpdir(), join(tmpdir(), "static-next"), join(tmpdir(), "a", "..", "b")])(
    "refuses artifact root %s", root => {
      expect(() => createHostStaticNextExecutor({ ...config(), LEAF_STATIC_NEXT_ARTIFACT_ROOT: root }, clients()))
        .toThrow("static_next_config_invalid: LEAF_STATIC_NEXT_ARTIFACT_ROOT");
    });

  it("returns the executor, durable resolver and artifact root with injected clients", async () => {
    const deps = clients();
    const env = config();
    const result = createHostStaticNextExecutor(env, deps)!;
    expect(Object.keys(result).sort()).toEqual(["artifactRoot", "custodyStore", "executor", "resolveStaticNextBundle"]);
    expect(result.artifactRoot).toBe(env.LEAF_STATIC_NEXT_ARTIFACT_ROOT);
    expect(result.executor).toMatchObject({ bindingId: env.LEAF_STATIC_NEXT_BINDING_ID,
      toolchainSha256: env.LEAF_STATIC_NEXT_TOOLCHAIN_SHA256, isolation: "filesystem-and-network" });
    expect(deps.s3Client.send).not.toHaveBeenCalled();
    expect(deps.codeBuildClient.send).not.toHaveBeenCalled();
    const executionId = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
    deps.s3Client.send.mockResolvedValue({ Body: {
      transformToByteArray: async () => Buffer.from(JSON.stringify({ executionId, bundleSha256: "b".repeat(64) })),
    } });
    await expect(result.resolveStaticNextBundle(executionId)).resolves.toBe("b".repeat(64));
    expect(deps.s3Client.send).toHaveBeenCalledTimes(1);
    expect(deps.s3Client.send.mock.calls[0][0]).toBeInstanceOf(GetObjectCommand);
    expect(deps.s3Client.send.mock.calls[0][0].input).toEqual({ Bucket: env.LEAF_STATIC_NEXT_BUCKET,
      Key: `map/${executionId}.json` });
  });

  it("allows a sibling path whose name starts with the temporary directory name", () => {
    const root = `${resolve(tmpdir())}-private`;
    expect(createHostStaticNextExecutor({ ...config(), LEAF_STATIC_NEXT_ARTIFACT_ROOT: root }, clients())?.artifactRoot)
      .toBe(root);
  });
});
