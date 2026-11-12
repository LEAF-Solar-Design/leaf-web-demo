import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { ProjectPhotoInspection, photoRequest, projectPhotoResult, PHOTO_FAILURE } from "../src/agent/projectPhotoInspection.js";

const org = "11111111-1111-4111-8111-111111111111";
const project = "22222222-2222-4222-8222-222222222222";
const id = (n: number) => `33333333-3333-4333-8333-${String(n).padStart(12, "0")}`;
const bytes = Buffer.from([255, 216, 255, ...Buffer.from("PRIVATE_PIXEL_MARKER"), 255, 217]);
const hash = createHash("sha256").update(bytes).digest("hex");
const authority = { orgId: org, sessionId: "app-session", turnId: "app-turn" };
const common = (saved = false) => ({ org_id: org, project_id: project, session_id: "app-session", turn_id: "app-turn",
  source: { project_id: project, revision: saved ? 7 : 0, digest: saved ? "a".repeat(64) : null },
  assignment_basis: saved ? "saved_project_page" : "no_saved_page" });
const labels = [{ gallery_slug: "cakes", gallery_title: "Cakes", index: 0, hero: true, caption: "Cake", alt: "A cake" }];
const row = (n: number, saved = false) => ({ material_id: id(n), name: "cake.jpg", media_type: "image/jpeg",
  byte_size: 200, sha256: "b".repeat(64), preview_sha256: hash, width: 10, height: 20,
  created_at: "2026-11-01T12:00:00+00:00", assignments: saved ? labels : [] });
const catalog = (saved = false) => ({ ...common(saved), schema: "leaf.project-material-catalog.v1",
  materials: [row(1, saved)], next_cursor: null, page_count: 1 });
const image = (saved = false) => ({ ...common(saved), schema: "leaf.project-material-image.v1", material_id: id(1),
  original_sha256: "b".repeat(64), preview_sha256: hash, media_type: "image/jpeg", sha256: hash,
  byte_size: bytes.length, data: bytes.toString("base64"), assignments: saved ? labels : [] });
const request = (saved = false) => ({ op: "preview", material_id: id(1), expected_preview_sha256: hash,
  expected_revision: saved ? 7 : 0, expected_digest: saved ? "a".repeat(64) : null });

describe("closed project photo consumer", () => {
  it.each([false, true])("emits true JPEG blocks only after catalog validation (saved=%s)", async saved => {
    const helper = new ProjectPhotoInspection(authority);
    const first = await helper.inspect({ op: "catalog" }, async () => catalog(saved));
    expect(first.isError).toBeUndefined();
    const result = await helper.inspect(request(saved), async () => image(saved));
    expect(result.isError).toBeUndefined();
    expect(JSON.stringify(result)).not.toContain(bytes.toString("base64"));
    expect(projectPhotoResult(result).content).toEqual([
      { type: "text", text: result.content }, { type: "image", mimeType: "image/jpeg", data: bytes.toString("base64") },
    ]);
    expect(projectPhotoResult({ ...result }).isError).toBe(true);
  });

  it("traverses all 94 stable IDs in two pages, without truncation", async () => {
    const helper = new ProjectPhotoInspection(authority);
    const first = { ...catalog(), materials: Array.from({ length: 50 }, (_, n) => row(n + 1)), next_cursor: id(50), page_count: 50 };
    const second = { ...catalog(), materials: Array.from({ length: 44 }, (_, n) => row(n + 51)), page_count: 44 };
    const a = await helper.inspect({ op: "catalog" }, async () => first);
    const b = await helper.inspect({ op: "catalog", cursor: id(50), expected_revision: 0, expected_digest: null }, async () => second);
    expect(JSON.parse(a.content).materials.length + JSON.parse(b.content).materials.length).toBe(94);
    const last = await helper.inspect({ ...request(), material_id: id(94) }, async () => ({ ...image(), material_id: id(94) }));
    expect(projectPhotoResult(last).content.filter(block => block.type === "image")).toHaveLength(1);
  });

  it.each([
    {}, { op: "original" }, { op: "catalog", project_id: project }, { op: "catalog", url: "https://bad.invalid" },
    { op: "catalog", cursor: id(1) }, { op: "catalog", expected_revision: 0, expected_digest: hash },
    { op: "catalog", expected_revision: 1, expected_digest: null }, { op: "catalog", expected_digest: null },
    { ...request(), material_id: id(1).toUpperCase().replace("3333", "AAAA") },
    { ...request(), expected_preview_sha256: hash.toUpperCase() }, { ...request(), cursor: id(1) },
    { op: "catalog", data: "x".repeat(4096) },
  ])("rejects invalid or authority-bearing bodies %#", body => {
    expect(() => photoRequest(body)).toThrow(PHOTO_FAILURE);
  });

  it.each(["org", "project", "turn", "source", "assignments", "extra", "duplicate", "count", "cursor", "rows", "url"])(
    "rejects catalog %s drift and arbitrary fields", async kind => {
      const value: Record<string, unknown> = catalog();
      if (kind === "org") value.org_id = project;
      if (kind === "project") value.project_id = org;
      if (kind === "turn") value.turn_id = "other-turn";
      if (kind === "source") value.source = { project_id: project, revision: 0, digest: hash };
      if (kind === "assignments") value.materials = [{ ...row(1), assignments: labels }];
      if (kind === "extra") value.storage_key = "RAW_BACKEND_MARKER";
      if (kind === "duplicate") { value.materials = [row(1), row(1)]; value.page_count = 2; }
      if (kind === "count") value.page_count = 2;
      if (kind === "cursor") value.next_cursor = id(2);
      if (kind === "rows") { value.materials = Array.from({ length: 51 }, (_, n) => row(n)); value.page_count = 51; }
      if (kind === "url") value.materials = [{ ...row(1), url: "https://storage.invalid" }];
      const result = await new ProjectPhotoInspection(authority).inspect({ op: "catalog" }, async () => value);
      expect(result).toEqual({ content: PHOTO_FAILURE, isError: true });
      expect(projectPhotoResult(result).content.every(block => block.type === "text")).toBe(true);
    },
  );

  it.each(["data", "base64", "signature", "size", "oversize", "media", "hash", "original", "member", "source", "project", "turn", "assignments", "extra"])(
    "never emits pixels for invalid preview %s", async kind => {
      const helper = new ProjectPhotoInspection(authority);
      await helper.inspect({ op: "catalog" }, async () => catalog());
      const value: Record<string, unknown> = image();
      if (kind === "data") value.data = Buffer.from("RAW_BACKEND_MARKER").toString("base64");
      if (kind === "base64") value.data = `${image().data}\n`;
      if (kind === "signature") value.data = Buffer.alloc(bytes.length).toString("base64");
      if (kind === "size") value.byte_size = 1;
      if (kind === "oversize") { value.data = Buffer.alloc(1024 * 1024 + 1).toString("base64"); value.byte_size = 1024 * 1024 + 1; }
      if (kind === "media") value.media_type = "image/png";
      if (kind === "hash") value.sha256 = "c".repeat(64);
      if (kind === "original") value.original_sha256 = "c".repeat(64);
      if (kind === "member") value.material_id = id(2);
      if (kind === "source") value.source = { project_id: project, revision: 1, digest: hash };
      if (kind === "project") value.project_id = org;
      if (kind === "turn") value.turn_id = "other-turn";
      if (kind === "assignments") value.assignments = labels;
      if (kind === "extra") value.raw_error = "RAW_BACKEND_MARKER";
      const result = await helper.inspect(request(), async () => value);
      expect(result).toEqual({ content: PHOTO_FAILURE, isError: true });
      expect(projectPhotoResult(result).content).toEqual([{ type: "text", text: PHOTO_FAILURE }]);
    },
  );

  it("refuses uncatalogued previews and stale continuation before reading", async () => {
    const read = vi.fn(async () => image());
    const helper = new ProjectPhotoInspection(authority);
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read).not.toHaveBeenCalled();
    await helper.inspect({ op: "catalog" }, async () => catalog());
    expect((await helper.inspect({ op: "catalog", cursor: id(1), expected_revision: 0, expected_digest: null }, read)).isError).toBe(true);
    expect(read).not.toHaveBeenCalled();
  });

  it.each(["project", "source", "duplicate", "turn"])("invalidates the turn witness on continuation %s drift", async kind => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspect({ op: "catalog" }, async () => ({ ...catalog(), next_cursor: id(1) }));
    const second = { ...catalog(), materials: [row(2)] };
    if (kind === "project") { second.project_id = org; second.source.project_id = org; }
    if (kind === "source") { second.source.revision = 1; second.source.digest = hash; }
    if (kind === "duplicate") second.materials = [row(1)];
    if (kind === "turn") second.turn_id = "another-turn";
    const result = await helper.inspect({ op: "catalog", cursor: id(1), expected_revision: 0, expected_digest: null }, async () => second);
    expect(result.isError).toBe(true);
    const retry = vi.fn(async () => catalog());
    expect((await helper.inspect({ op: "catalog" }, retry)).isError).toBe(true);
    expect(retry).not.toHaveBeenCalled();
  });
});
