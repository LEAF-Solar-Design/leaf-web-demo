import { describe, expect, it, vi } from "vitest";
import { canonicalVideo, SdkProjectVideoInspector, validateProjectVideo, validateVideoReport,
  VideoInspectionError, videoHash, type VideoIdentity } from "../src/agent/projectVideoInspection.js";
import { jpeg, videoFixture } from "./projectVideoFixture.js";

const identity: VideoIdentity = {
  tenant_id: "11111111-1111-4111-8111-111111111111",
  organization_id: "22222222-2222-4222-8222-222222222222",
  project_id: "33333333-3333-4333-8333-333333333333",
  actor_binding_id: "66666666-6666-4666-8666-666666666666",
  edit_id: "55555555-5555-4555-8555-555555555555",
};
const grant = { kind: "api_key" as const, apiKey: "synthetic-inert-key-for-fixture" };

describe("private video image producer", () => {
  it("validates exact selected transcripts, immutable manifest and actual image bytes", () => {
    const f = videoFixture(identity);
    const parsed = validateProjectVideo(f.video, identity, f.materials);
    expect(parsed).toEqual(f.video);
    expect(validateVideoReport(f.report, parsed)).toEqual(f.report);
    expect(Buffer.from(parsed.images[0]!.data, "base64")).toEqual(jpeg);
  });

  it.each(["tenant", "actor", "digest", "pixels", "frame-id", "missing-image", "transcript", "missing-transcript",
    "extra-transcript", "timestamps", "coverage", "extra-field"])("rejects %s before SDK use", mode => {
    const f = videoFixture(identity);
    if (mode === "tenant") f.video.manifest.tenant_id = identity.project_id;
    if (mode === "actor") f.video.manifest.actor_binding_id = identity.project_id;
    if (mode === "pixels") f.video.images[0]!.data = Buffer.from("not an image").toString("base64");
    if (mode === "frame-id") f.video.images[0]!.frame_id = "frame-099";
    if (mode === "missing-image") f.video.images.pop();
    if (mode === "transcript") f.materials.set([...f.materials.keys()][0]!, Buffer.from("changed"));
    if (mode === "missing-transcript") f.materials.clear();
    if (mode === "extra-transcript") f.materials.set("extra.txt", Buffer.from("extra"));
    if (mode === "timestamps") f.video.manifest.frames[1]!.timestamp_ms = 3000;
    if (mode === "coverage") f.video.manifest.duration_ms = 9000;
    if (mode === "extra-field") Object.assign(f.video.manifest, { arbitrary_path: "/private" });
    f.video.manifest_sha256 = mode === "digest" ? "0".repeat(64) : videoHash(canonicalVideo(f.video.manifest));
    expect(() => validateProjectVideo(f.video, identity, f.materials)).toThrow(VideoInspectionError);
  });

  it("delivers structured pixels plus complete transcript using one tool-free SDK query", async () => {
    const f = videoFixture(identity);
    let options: Record<string, unknown> = {}, messages: any[] = [];
    const query = vi.fn(async function* (input) {
      options = input.options;
      for await (const message of input.prompt) messages.push(message);
      yield { type: "result", subtype: "success", result: JSON.stringify(f.notes),
        usage: { input_tokens: 7, output_tokens: 5 } };
    });
    const inspector = new SdkProjectVideoInspector({ sdkImport: async () => ({ query }) });
    const result = await inspector.inspect({ video: f.video, transcripts: f.materials, grant,
      signal: new AbortController().signal });
    expect(query).toHaveBeenCalledOnce();
    expect(options.tools).toEqual([]); expect(options.allowedTools).toEqual([]); expect(options.mcpServers).toEqual({});
    expect(options.maxTurns).toBe(1); expect(options.settingSources).toEqual([]);
    expect(messages).toHaveLength(1);
    const blocks = messages[0].message.content;
    expect(blocks.filter((b: any) => b.type === "image").map((b: any) => b.source.data))
      .toEqual(f.video.images.map(row => row.data));
    expect(blocks.some((b: any) => b.type === "text" && b.text.includes("Synthetic transcript.\n"))).toBe(true);
    expect(result.telemetry).toEqual({ input_tokens: 7, output_tokens: 5 });
    expect(validateVideoReport(result.report, f.video)).toEqual(result.report);
  });

  it.each(["missing", "duplicate", "foreign", "malformed", "oversized", "nonterminal", "sdk-error", "secret", "escaped-secret"])(
    "refuses %s inspection without losing terminal usage", async mode => {
      const f = videoFixture(identity), notes = structuredClone(f.notes);
      if (mode === "missing") notes.observations.pop();
      if (mode === "duplicate") notes.observations[1]!.frame_id = notes.observations[0]!.frame_id;
      if (mode === "foreign") notes.observations[0]!.frame_id = "frame-099";
      const query = vi.fn(async function* () {
        if (mode === "sdk-error") throw new Error(grant.apiKey);
        if (mode === "nonterminal") return;
        if (mode === "escaped-secret") notes.summary = grant.apiKey;
        yield { type: "result", subtype: "success", usage: { input_tokens: 9, output_tokens: 4 },
          result: mode === "malformed" ? "invalid" : mode === "oversized" ? "x".repeat(300000)
            : mode === "secret" ? grant.apiKey : mode === "escaped-secret"
              ? JSON.stringify(notes).replace(grant.apiKey, "\\u0073" + grant.apiKey.slice(1)) : JSON.stringify(notes) };
      });
      const inspector = new SdkProjectVideoInspector({ sdkImport: async () => ({ query }) });
      const error = await inspector.inspect({ video: f.video, transcripts: f.materials, grant,
        signal: new AbortController().signal }).catch(e => e);
      expect(error).toBeInstanceOf(VideoInspectionError);
      expect(error.message).not.toContain(grant.apiKey);
      if (!["nonterminal", "sdk-error"].includes(mode)) expect(error.telemetry).toEqual({ input_tokens: 9, output_tokens: 4 });
      expect(query).toHaveBeenCalledOnce();
    });

  it("does not import or execute the SDK after cancellation", async () => {
    const f = videoFixture(identity), controller = new AbortController(), sdkImport = vi.fn();
    controller.abort();
    await expect(new SdkProjectVideoInspector({ sdkImport }).inspect({
      video: f.video, transcripts: f.materials, grant, signal: controller.signal,
    })).rejects.toThrow(VideoInspectionError);
    expect(sdkImport).not.toHaveBeenCalled();
  });
});
