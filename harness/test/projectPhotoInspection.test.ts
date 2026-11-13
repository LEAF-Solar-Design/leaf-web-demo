import { createHash } from "node:crypto";
import { describe, expect, it, vi } from "vitest";
import { ProjectPhotoInspection, photoRequest, projectPhotoResult, PHOTO_FAILURE, PHOTO_MAX_RESPONSE, type ProjectPhotoInspectionInput } from "../src/agent/projectPhotoInspection.js";
import { ProjectPhotoClientError } from "../src/ports/impl/appRunClient.js";

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
  it("accepts a bounded derivative while preserving immutable assignment witnesses", async () => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspect({ op: "catalog" }, async () => catalog(true));
    const derivative = Buffer.from([255, 216, 255, 1, 2, 3, 255, 217]);
    const result = await helper.inspect(request(true), async () => ({ ...image(true),
      data: derivative.toString("base64"), byte_size: derivative.length,
      inspection_sha256: createHash("sha256").update(derivative).digest("hex"), derived_from_sha256: hash }));
    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content).preview_sha256).toBe(hash);
    expect(JSON.parse(result.content).assignments).toEqual(labels);
    expect(projectPhotoResult(result).content).toContainEqual({ type: "image", mimeType: "image/jpeg", data: derivative.toString("base64") });
    expect(helper.reconciliation().inspected_ids).toEqual([id(1)]);
  });

  it.each(["missing_digest", "missing_source", "wrong_source", "wrong_bytes", "wrong_saved_preview", "foreign_project", "extra_field"])(
    "rejects invalid inspection derivative %s without emitting pixels", async kind => {
      const helper = new ProjectPhotoInspection(authority);
      await helper.inspect({ op: "catalog" }, async () => catalog());
      const payload: Record<string, unknown> = { ...image(), inspection_sha256: hash, derived_from_sha256: hash };
      if (kind === "missing_digest") delete payload.inspection_sha256;
      if (kind === "missing_source") delete payload.derived_from_sha256;
      if (kind === "wrong_source") payload.derived_from_sha256 = "c".repeat(64);
      if (kind === "wrong_bytes") payload.inspection_sha256 = "c".repeat(64);
      if (kind === "wrong_saved_preview") payload.sha256 = "c".repeat(64);
      if (kind === "foreign_project") payload.project_id = id(9);
      if (kind === "extra_field") payload.unknown = "unexpected";
      const result = await helper.inspect(request(), async () => payload);
      expect(result.isError).toBe(true);
      expect(projectPhotoResult(result).content.some(block => block.type === "image")).toBe(false);
      expect(helper.reconciliation().inspected_ids).toEqual([]);
    });

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

  it("automatically pages the catalog and delivers actual pixels for every original", async () => {
    const helper = new ProjectPhotoInspection(authority);
    const read = vi.fn(async (body: ProjectPhotoInspectionInput) => body.op === "catalog"
      ? { ...catalog(), materials: Array.from({ length: body.cursor ? 44 : 50 }, (_, n) => row(n + (body.cursor ? 51 : 1))),
        next_cursor: body.cursor ? null : id(50), page_count: body.cursor ? 44 : 50 }
      : { ...image(), material_id: body.material_id });
    const result = await helper.inspectCatalog({ op: "catalog" }, read);
    expect(result.isError).toBeUndefined();
    expect(read.mock.calls.map(([body]) => body)).toEqual([
      { op: "catalog" }, { op: "catalog", cursor: id(50), expected_revision: 0, expected_digest: null },
    ]);
    const all = JSON.parse(result.content);
    expect(all.materials.map((item: { material_id: string }) => item.material_id)).toEqual(
      Array.from({ length: 94 }, (_, n) => id(n + 1)));
    expect(all.next_cursor).toBeNull();
    const previews = await Promise.all(all.materials.map((item: { material_id: string }) =>
      helper.inspect({ ...request(), material_id: item.material_id }, read)));
    for (const inspected of previews) expect(projectPhotoResult(inspected).content.filter(block => block.type === "image")).toHaveLength(1);
    expect(helper.reconciliation().inspected_ids).toHaveLength(all.materials.length);
    expect(helper.reconciliation().catalog_complete).toBe(true);
  });

  it("applies the transport byte bound per page rather than truncating a larger catalog", async () => {
    const helper = new ProjectPhotoInspection(authority);
    const rows = Array.from({ length: 2500 }, (_, n) => ({ ...row(n + 1), name: "x".repeat(255) }));
    const read = vi.fn(async (body: ProjectPhotoInspectionInput) => {
      const start = body.cursor ? rows.findIndex(row => row.material_id === body.cursor) + 1 : 0;
      const materials = rows.slice(start, start + 50);
      const page = { ...catalog(), materials, page_count: materials.length,
        next_cursor: start + materials.length < rows.length ? materials.at(-1)!.material_id : null };
      expect(Buffer.byteLength(JSON.stringify(page))).toBeLessThan(PHOTO_MAX_RESPONSE);
      return page;
    });
    const result = await helper.inspectCatalog({ op: "catalog" }, read);
    expect(result.isError).toBeUndefined();
    expect(Buffer.byteLength(result.content)).toBeGreaterThan(PHOTO_MAX_RESPONSE);
    expect(JSON.parse(result.content).materials).toEqual(rows);
    expect(read).toHaveBeenCalledTimes(rows.length / 50);
    expect(helper.reconciliation().catalog_complete).toBe(true);
  });

  it("retries a transient preview only after reacquiring catalog and source witnesses", async () => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspect({ op: "catalog" }, async () => catalog(true));
    const calls: unknown[] = [];
    let previews = 0;
    const result = await helper.inspect(request(true), async body => {
      calls.push(body);
      if (body.op === "catalog") return catalog(true);
      if (++previews === 1) throw new ProjectPhotoClientError("transport");
      return image(true);
    });
    expect(calls).toEqual([request(true), { op: "catalog", expected_revision: 7, expected_digest: "a".repeat(64) }, request(true)]);
    expect(projectPhotoResult(result).content.filter(block => block.type === "image")).toHaveLength(1);
    expect(helper.reconciliation().inspected_ids).toEqual([id(1)]);
    expect(JSON.stringify(result)).not.toContain("PRIVATE transport detail");
  });

  it("bounds attempts for a failing photo and continues with other members", async () => {
    const helper = new ProjectPhotoInspection(authority);
    const two = { ...catalog(), materials: [row(1), row(2)], page_count: 2 };
    await helper.inspect({ op: "catalog" }, async () => two);
    const read = vi.fn(async (body: ProjectPhotoInspectionInput) => {
      if (body.op === "catalog") return two;
      if (body.material_id === id(1)) throw new ProjectPhotoClientError("transport");
      return { ...image(), material_id: id(2) };
    });
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read.mock.calls.filter(([body]) => body.op === "preview")).toHaveLength(3);
    expect(read.mock.calls.filter(([body]) => body.op === "catalog")).toHaveLength(2);
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read).toHaveBeenCalledTimes(5);
    expect((await helper.inspect({ ...request(), material_id: id(2) }, read)).isError).toBeUndefined();
    expect(helper.reconciliation().unresolved).toEqual([
      { material_id: id(1), reason: "transport_exhausted" }, { material_id: id(2), reason: "inspected_not_proposed" },
    ]);
  });

  it.each(["source", "original", "preview"])("stops retrying when refreshing reveals changed %s witnesses", async kind => {
    const helper = new ProjectPhotoInspection(authority);
    const two = { ...catalog(), materials: [row(1), row(2)], page_count: 2 };
    await helper.inspectCatalog({ op: "catalog" }, async () => two);
    const read = vi.fn(async (body: ProjectPhotoInspectionInput) => {
      if (body.op === "preview") throw new ProjectPhotoClientError("transport");
      return { ...two, ...(kind === "source" ? common(true) : {}), materials: [
        { ...row(1), ...(kind === "original" ? { sha256: "d".repeat(64) }
          : kind === "preview" ? { preview_sha256: "d".repeat(64) } : {}) }, row(2),
      ] };
    });
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read.mock.calls.map(([body]) => body.op)).toEqual(["preview", "catalog"]);
    expect(helper.reconciliation().unresolved[0]).toEqual({ material_id: id(1), reason: "source_changed" });
    expect((await helper.inspect({ ...request(), material_id: id(2) }, async () => ({ ...image(), material_id: id(2) }))).isError).toBeUndefined();
  });

  it("retries typed transport failures on a catalog continuation without dropping earlier members", async () => {
    const helper = new ProjectPhotoInspection(authority);
    let continuations = 0;
    const read = vi.fn(async (body: ProjectPhotoInspectionInput) => {
      if (!body.cursor) return { ...catalog(), next_cursor: id(1) };
      if (++continuations < 3) throw new ProjectPhotoClientError("transport");
      return { ...catalog(), materials: [row(2)] };
    });
    const result = await helper.inspectCatalog({ op: "catalog" }, read);
    expect(result.isError).toBeUndefined();
    expect(JSON.parse(result.content).materials.map((row: { material_id: string }) => row.material_id)).toEqual([id(1), id(2)]);
    expect(read).toHaveBeenCalledTimes(4);
    expect(helper.reconciliation().catalog_complete).toBe(true);
  });

  it.each([403, 409, 422, 502, 503])("does not retry opaque failures, even transient-looking statuses (%s)", async status => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspect({ op: "catalog" }, async () => ({ ...catalog(), materials: [row(1), row(2)], page_count: 2 }));
    const read = vi.fn(async () => { throw Object.assign(new Error("PRIVATE failure"), { status }); });
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read).toHaveBeenCalledOnce();
    expect((await helper.inspect({ ...request(), material_id: id(2) }, async () => ({ ...image(), material_id: id(2) }))).isError).toBeUndefined();
    expect(helper.reconciliation().unresolved[0]).toEqual({ material_id: id(1),
      reason: status === 403 ? "access_denied" : status === 409 ? "source_changed" : status >= 500 ? "unavailable" : "integrity_failure" });
  });

  it.each([
    ["authorization", "access_denied"], ["integrity", "integrity_failure"],
    ["source_changed", "source_changed"], ["unavailable", "unavailable"],
  ] as const)("reports typed %s failures without retrying or blocking another photo", async (kind, reason) => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspectCatalog({ op: "catalog" }, async () => ({ ...catalog(), materials: [row(1), row(2)], page_count: 2 }));
    const read = vi.fn(async () => { throw new ProjectPhotoClientError(kind); });
    const result = await helper.inspect(request(), read);
    expect(result.isError).toBe(true);
    expect(read).toHaveBeenCalledOnce();
    expect(projectPhotoResult(result).content).toEqual([{ type: "text", text: JSON.stringify({
      error: PHOTO_FAILURE, material_id: id(1), reason, reconciliation: helper.reconciliation(),
    }) }]);
    expect((await helper.inspect({ ...request(), material_id: id(2) }, async () => ({ ...image(), material_id: id(2) }))).isError).toBeUndefined();
    expect(helper.reconciliation().unresolved[0]).toEqual({ material_id: id(1), reason });
  });

  it.each([{ code: "ECONNRESET" }, { name: "AbortError" }])("does not retry an untyped transport-looking exception %s", async properties => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspectCatalog({ op: "catalog" }, async () => catalog());
    const read = vi.fn(async () => { throw Object.assign(new Error("PRIVATE"), properties); });
    expect((await helper.inspect(request(), read)).isError).toBe(true);
    expect(read).toHaveBeenCalledOnce();
  });

  it("never retries bad pixels or a changed source discovered while refreshing", async () => {
    for (const changedSource of [false, true]) {
      const helper = new ProjectPhotoInspection(authority);
      await helper.inspect({ op: "catalog" }, async () => catalog(true));
      const read = vi.fn(async (body: ProjectPhotoInspectionInput) => {
        if (body.op === "catalog") return { ...catalog(true), source: { ...common(true).source, revision: 8 } };
        if (changedSource) throw new ProjectPhotoClientError("transport");
        return { ...image(true), sha256: "d".repeat(64) };
      });
      expect((await helper.inspect(request(true), read)).isError).toBe(true);
      expect(read.mock.calls.filter(([body]) => body.op === "preview")).toHaveLength(1);
      expect(helper.reconciliation().inspected_ids).toEqual([]);
    }
  });

  it("reconciles saved, inspected, proposed and unresolved IDs without using filenames", async () => {
    const helper = new ProjectPhotoInspection(authority);
    await helper.inspectCatalog({ op: "catalog" }, async () => ({ ...catalog(true),
      materials: [row(1, true), row(2), row(3), row(4)], page_count: 4 }));
    await helper.inspect({ ...request(true), material_id: id(2) }, async () => ({ ...image(true), material_id: id(2), assignments: [] }));
    await helper.inspect({ ...request(true), material_id: id(3) }, async () => ({ ...image(true), material_id: id(3), assignments: [] }));
    expect(helper.reconciliation([id(2)])).toEqual({
      catalog_complete: true, catalogued_ids: [id(1), id(2), id(3), id(4)], inspected_ids: [id(2), id(3)],
      proposed_ids: [id(2)], saved_ids: [id(1)], unresolved: [
        { material_id: id(2), reason: "awaiting_explicit_save" },
        { material_id: id(3), reason: "inspected_not_proposed" },
        { material_id: id(4), reason: "not_inspected" },
      ],
    });
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
      const projected = projectPhotoResult(result).content;
      expect(projected).toHaveLength(1);
      expect(projected[0]!.type).toBe("text");
      if (kind === "project" || kind === "turn") {
        expect(projected).toEqual([{ type: "text", text: PHOTO_FAILURE }]);
        expect(helper.reconciliation().unresolved).toEqual([{ material_id: id(1), reason: "integrity_failure" }]);
      } else expect(JSON.parse((projected[0] as { text: string }).text)).toMatchObject({
        error: PHOTO_FAILURE, material_id: id(1), reason: "integrity_failure",
      });
      expect(JSON.stringify(projected)).not.toContain("RAW_BACKEND_MARKER");
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
