import { GetObjectCommand, PutObjectCommand } from "@aws-sdk/client-s3";
import { BatchGetBuildsCommand, StartBuildCommand, StopBuildCommand } from "@aws-sdk/client-codebuild";
import { describe, expect, it, vi } from "vitest";
import { createCodeBuildStaticNextBuildProvider, createS3StaticNextObjectStore } from "../src/projectBuild/staticNextAwsAdapters.js";

const executionId = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";
const key = `map/${executionId}.json`;
function s3() {
  const send = vi.fn();
  return { send, store: createS3StaticNextObjectStore({ client: { send }, bucket: "private-bucket" }) };
}

describe("static Next S3 adapter", () => {
  it("conditionally creates encrypted objects", async () => {
    const f = s3();
    const body = Buffer.from("record");
    await expect(f.store.putIfAbsent(key, body)).resolves.toBe("created");
    expect(f.send).toHaveBeenCalledTimes(1);
    expect(f.send.mock.calls[0][0]).toBeInstanceOf(PutObjectCommand);
    expect(f.send.mock.calls[0][0].input).toEqual({ Bucket: "private-bucket", Key: key, Body: body,
      IfNoneMatch: "*", ServerSideEncryption: "AES256" });
  });

  it.each([{ name: "PreconditionFailed" }, { $metadata: { httpStatusCode: 412 } }])(
    "preserves an existing object on %j", async error => {
      const f = s3();
      f.send.mockRejectedValue(error);
      await expect(f.store.putIfAbsent(key, Buffer.from("record"))).resolves.toBe("exists");
      expect(f.send).toHaveBeenCalledTimes(1);
    });

  it.each([{ name: "ConditionalRequestConflict" }, { $metadata: { httpStatusCode: 409 } }])(
    "retries a conditional conflict once: %j", async error => {
      const f = s3();
      f.send.mockRejectedValueOnce(error).mockResolvedValueOnce({});
      await expect(f.store.putIfAbsent(key, Buffer.from("record"))).resolves.toBe("created");
      expect(f.send).toHaveBeenCalledTimes(2);
      f.send.mockClear().mockRejectedValue(error);
      await expect(f.store.putIfAbsent(key, Buffer.from("record"))).resolves.toBe("exists");
      expect(f.send).toHaveBeenCalledTimes(2);
    });

  it("reads a streamed object as a Buffer", async () => {
    const f = s3();
    const transformToByteArray = vi.fn(async () => new Uint8Array(Buffer.from("onetwo")));
    f.send.mockResolvedValue({ Body: { transformToByteArray } });
    await expect(f.store.get(key)).resolves.toEqual(Buffer.from("onetwo"));
    expect(transformToByteArray).toHaveBeenCalledOnce();
    expect(f.send.mock.calls[0][0]).toBeInstanceOf(GetObjectCommand);
    expect(f.send.mock.calls[0][0].input).toEqual({ Bucket: "private-bucket", Key: key });
  });

  it.each([{ name: "NoSuchKey" }, { $metadata: { httpStatusCode: 404 } }])("returns null for %j", async error => {
    const f = s3();
    f.send.mockRejectedValue(error);
    await expect(f.store.get(key)).resolves.toBeNull();
  });

  it("propagates other AWS errors", async () => {
    const f = s3();
    const error = new Error("access denied");
    f.send.mockRejectedValue(error);
    await expect(f.store.get(key)).rejects.toBe(error);
    await expect(f.store.putIfAbsent(key, Buffer.from("record"))).rejects.toBe(error);
  });

  it.each(["in", "out", "map"])("accepts digest keys under %s", async prefix => {
    const f = s3();
    const digestKey = `${prefix}/${executionId}.json`;
    f.send.mockResolvedValue({ Body: { transformToByteArray: async () => new Uint8Array() } });
    await expect(f.store.putIfAbsent(digestKey, Buffer.from("record"))).resolves.toBe("created");
    await expect(f.store.get(digestKey)).resolves.toEqual(Buffer.alloc(0));
    expect(f.send).toHaveBeenCalledTimes(2);
  });

  it.each(["../map/id.json", `other/${executionId}.json`, "map/00000000-0000-0000-0000-000000000001.json",
    `map/${"a".repeat(63)}.json`, `map/${"a".repeat(65)}.json`, `map/${"g".repeat(64)}.json`,
    `in/${executionId.toUpperCase().replace(/1$/, "A")}.json`, `out/${executionId}.json/extra`,
    `map/${executionId}.json\n`])("rejects bad key %s before sending", async badKey => {
      const f = s3();
      await expect(f.store.get(badKey)).rejects.toThrow("static_next_key_invalid");
      await expect(f.store.putIfAbsent(badKey, Buffer.from("record"))).rejects.toThrow("static_next_key_invalid");
      expect(f.send).not.toHaveBeenCalled();
    });
});

describe("static Next CodeBuild adapter", () => {
  function fixture(projectName = "static-next") {
    const send = vi.fn();
    return { send, provider: createCodeBuildStaticNextBuildProvider({ client: { send }, projectName }) };
  }

  it("starts the selected project with the execution ID", async () => {
    const f = fixture();
    f.send.mockResolvedValue({ build: { id: "project:build" } });
    await expect(f.provider.start(executionId)).resolves.toBe("project:build");
    expect(f.send.mock.calls[0][0]).toBeInstanceOf(StartBuildCommand);
    expect(f.send.mock.calls[0][0].input).toEqual({ projectName: "static-next",
      idempotencyToken: expect.stringMatching(/^[a-f0-9]{64}$/),
      environmentVariablesOverride: [{ name: "EXECUTION_ID", value: executionId, type: "PLAINTEXT" }] });
  });

  it("reuses the token for a retry and separates execution and project identities", async () => {
    const original = fixture();
    const restarted = fixture();
    const otherProject = fixture("different-static-next");
    for (const f of [original, restarted, otherProject]) {
      f.send.mockResolvedValue({ build: { id: "project:build" } });
    }
    // Distinct execution digests for chart data, gallery captions and report export.
    const executions = ["a", "b", "c"].map(character => character.repeat(64));
    const tokens: string[] = [];
    for (const id of executions) {
      await original.provider.start(id);
      await restarted.provider.start(id);
      await otherProject.provider.start(id);
      const token = original.send.mock.lastCall![0].input.idempotencyToken;
      expect(restarted.send.mock.lastCall![0].input.idempotencyToken).toBe(token);
      expect(otherProject.send.mock.lastCall![0].input.idempotencyToken).not.toBe(token);
      tokens.push(token);
    }
    expect(new Set(tokens).size).toBe(executions.length);
  });

  it("rejects a start response with no build ID", async () => {
    const f = fixture();
    f.send.mockResolvedValue({});
    await expect(f.provider.start(executionId)).rejects.toThrow("static_next_build_id_missing");
  });

  it.each(["QUEUED", "IN_PROGRESS", "SUCCEEDED", "FAILED", "FAULT", "TIMED_OUT", "STOPPED", "UNKNOWN"])(
    "maps status %s", async status => {
      const f = fixture();
      f.send.mockResolvedValue({ builds: [{ id: "project:build", buildStatus: status }] });
      await expect(f.provider.state("project:build")).resolves.toBe(status === "UNKNOWN" ? "FAULT" : status);
      expect(f.send.mock.calls[0][0]).toBeInstanceOf(BatchGetBuildsCommand);
      expect(f.send.mock.calls[0][0].input).toEqual({ ids: ["project:build"] });
    });

  it.each([{}, { buildStatus: "IN_PROGRESS", currentPhase: "SUBMITTED" },
    { buildStatus: "IN_PROGRESS", currentPhase: "QUEUED" },
    { buildStatus: "IN_PROGRESS", currentPhase: "PROVISIONING" }])(
    "maps a build awaiting execution to QUEUED: %j", async build => {
      const f = fixture();
      f.send.mockResolvedValue({ builds: [{ id: "project:build", ...build }] });
      await expect(f.provider.state("project:build")).resolves.toBe("QUEUED");
    });

  it.each([{}, { builds: [] }, { builds: [{ id: "other", buildStatus: "SUCCEEDED" }] }])(
    "returns FAULT for a missing build: %j", async response => {
      const f = fixture();
      f.send.mockResolvedValue(response);
      await expect(f.provider.state("project:build")).resolves.toBe("FAULT");
    });

  it("stops the requested build", async () => {
    const f = fixture();
    await f.provider.stop("project:build");
    expect(f.send.mock.calls[0][0]).toBeInstanceOf(StopBuildCommand);
    expect(f.send.mock.calls[0][0].input).toEqual({ id: "project:build" });
  });
});
