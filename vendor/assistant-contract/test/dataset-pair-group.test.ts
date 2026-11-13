import { describe, expect, it } from "vitest";
import { DatasetElementV1, DatasetReadV1 } from "../src/integration";

const element = { schema_version: 1, id: "capacity", title: "Capacity", kind: "dataset.table",
  dataset_ref: { id: "accounts", version: 1, digest: "a".repeat(64) },
  query: { fields: ["pair_key", "provider", "remaining", "reset"], limit: 100 },
  props: { columns: ["pair_key", "provider", "remaining", "reset"], row_limit: 100,
    row_grouping: { kind: "provider_pairs", pair_key_field: "pair_key", provider_field: "provider", providers: ["Alpha", "Beta"] } } };

describe("persisted provider-pair presentation", () => {
  it("preserves the optional strict grouping settings in a saved table recipe", () => {
    expect(DatasetElementV1.parse(element)).toEqual(element);
  });
  it("keeps existing plain table recipes unchanged", () => {
    const { row_grouping, ...props } = element.props;
    expect(DatasetElementV1.parse({ ...element, props })).toEqual({ ...element, props });
  });
  it.each([
    { kind: "provider_pairs", pair_key_field: "pair_key", provider_field: "provider", providers: ["Alpha", "Alpha"] },
    { ...element.props.row_grouping, providers: ["Alpha"] },
    { ...element.props.row_grouping, providers: ["Alpha", "Beta", "Gamma"] },
    { ...element.props.row_grouping, providers: ["", "Beta"] },
    { ...element.props.row_grouping, providers: [" ", "Beta"] },
    { ...element.props.row_grouping, providers: ["a\nb", "Beta"] },
    { ...element.props.row_grouping, pair_key_field: "provider" },
    { ...element.props.row_grouping, pair_key_field: "unrequested" },
    { ...element.props.row_grouping, provider_field: "unrequested" },
    { ...element.props.row_grouping, records: [] },
  ])("rejects malformed or unprojected grouping metadata %j", grouping => {
    expect(DatasetElementV1.safeParse({ ...element, props: { ...element.props, row_grouping: grouping } }).success).toBe(false);
  });
  it("requires both grouping fields among visible columns as well as requested fields", () => {
    expect(DatasetElementV1.safeParse({ ...element, props: { ...element.props, columns: ["provider", "remaining", "reset"] } }).success).toBe(false);
  });
  it("refuses table grouping on a line recipe", () => {
    expect(DatasetElementV1.safeParse({ ...element, kind: "dataset.line",
      props: { x_field: "reset", y_field: "remaining", row_grouping: element.props.row_grouping } }).success).toBe(false);
  });
});


const read = { schema_version: 1, dataset_ref: element.dataset_ref,
  fields: [{ id: "provider", label: "Provider", type: { kind: "text" }, nullable: false, sensitivity: "internal" }],
  rows: [{ key: "one", values: { provider: "Alpha" } }], status: "ready", next_cursor: null,
  freshness: { fetched_at: "2026-11-12T12:00:00Z", synced_at: null, source_updated_at: null } };
const coverage = { kind: "complete_materialization", matching_rows: 1, offset: 0 };
it("preserves explicit producer coverage and keeps older reads valid without it", () => {
  expect(DatasetReadV1.parse(read)).toEqual(read);
  expect(DatasetReadV1.parse({ ...read, query_coverage: coverage })).toEqual({ ...read, query_coverage: coverage });
});
it.each([
  { ...coverage, matching_rows: -1 }, { ...coverage, matching_rows: 100001 },
  { ...coverage, offset: -1 }, { ...coverage, offset: 2 },
  { ...coverage, matching_rows: 0 }, { ...coverage, matching_rows: 2 },
  { ...coverage, complete: true }, { ...coverage, kind: "connector_feed" },
])("refuses malformed or contradictory query coverage %j", query_coverage => {
  expect(DatasetReadV1.safeParse({ ...read, query_coverage }).success).toBe(false);
});
it("admits honest nonfirst terminal and partial pages without calling them complete", () => {
  expect(DatasetReadV1.safeParse({ ...read, query_coverage: { ...coverage, matching_rows: 2, offset: 1 } }).success).toBe(true);
  expect(DatasetReadV1.safeParse({ ...read, next_cursor: "next", query_coverage: { ...coverage, matching_rows: 2 } }).success).toBe(true);
});
