import React from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DatasetElement } from "../src/datasets/DatasetElement";
import { resolveDisplayPreferences } from "../src/datasets/formatting";
import { DatasetReadV1 } from "../../assistant-contract/src/integration";

afterEach(cleanup);
const reference = { id: "capacity", version: 1, digest: "a".repeat(64) };
const ids = ["account", "pair_key", "provider", "remaining", "reset"];
const element = { schema_version: 1, id: "capacity-view", title: "Capacity", kind: "dataset.table",
  dataset_ref: reference, query: { fields: ids, limit: 100 },
  props: { columns: ids, row_limit: 100, row_grouping: { kind: "provider_pairs", pair_key_field: "pair_key",
    provider_field: "provider", providers: ["Alpha", "Beta"] } } };
const row = (key: string, pair: string | null, provider: string, remaining = 17) => ({ key,
  values: { account: key, pair_key: pair, provider, remaining, reset: "2026-11-12T12:00:00Z" } });
function read(rows = [row("a", "match", "Alpha"), row("b", "match", "Beta", 29)]): DatasetReadV1 {
  return { schema_version: 1, dataset_ref: reference, status: "ready", next_cursor: null,
    fields: ids.map(id => ({ id, label: id, type: { kind: id === "remaining" ? "number" : id === "reset" ? "instant" : "text" },
      nullable: id === "pair_key", sensitivity: "internal" })), rows,
    query_coverage: { kind: "complete_materialization", matching_rows: rows.length, offset: 0 },
    freshness: { fetched_at: "2026-11-12T12:00:00Z", synced_at: null, source_updated_at: null } };
}
const show = (value: unknown, recipe: unknown = element) => render(<DatasetElement element={recipe} read={value} preferences={resolveDisplayPreferences()} />);
it("renders an explicit labelled pair with both original metrics and reset values", () => {
  const value = read(), before = JSON.stringify(value); show(value);
  const pair = screen.getByRole("region", { name: "Pair match" });
  expect(within(pair).getByRole("heading", { name: "Pair match" })).toBeTruthy();
  expect(within(pair).getByText("17")).toBeTruthy(); expect(within(pair).getByText("29")).toBeTruthy();
  expect(within(pair).getAllByText(/Nov 12, 2026/)).toHaveLength(2);
  expect(JSON.stringify(value)).toBe(before);
});
it("keeps a third provider separate even with the paired key, with all unpaired rows last", () => {
  show(read([row("third", "match", "Gamma"), row("a", "match", "Alpha"), row("b", "match", "Beta"), row("alone", "", "Alpha")]));
  const regions = screen.getAllByRole("region").map(node => node.getAttribute("aria-label"));
  expect(regions.indexOf("Pair match")).toBeLessThan(regions.indexOf("Unpaired accounts"));
  const singles = screen.getByRole("region", { name: "Unpaired accounts" });
  expect(within(singles).getByText("third")).toBeTruthy(); expect(within(singles).getByText("alone")).toBeTruthy();
});
it.each(["", " ", null])("does not pair blank/missing key %s", key => {
  show(read([row("a", key, "Alpha"), row("b", key, "Beta")]));
  expect(screen.queryByRole("region", { name: /^Pair / })).toBeNull();
  expect(within(screen.getByRole("region", { name: "Unpaired accounts" })).getAllByRole("row")).toHaveLength(3);
});
it("keeps duplicates, missing partners and distinct key case/space separate", () => {
  show(read([row("a1", "duplicate", "Alpha"), row("a2", "duplicate", "Alpha"), row("b", "duplicate", "Beta"),
    row("case", "X", "Alpha"), row("case2", "x", "Beta"), row("space", " x", "Alpha")]));
  expect(screen.queryByRole("region", { name: /^Pair / })).toBeNull();
  expect(within(screen.getByRole("region", { name: "Unpaired accounts" })).getAllByRole("row")).toHaveLength(7);
});
it.each(["absent", "query-limit", "last-page", "byte-limit", "table-limit", "stale"])("does not assert verified pairs for %s coverage", mode => {
  const value = read(); let recipe: unknown = element;
  if (mode === "absent") delete value.query_coverage;
  if (mode === "query-limit") { value.query_coverage!.matching_rows = 3; value.next_cursor = "next"; }
  if (mode === "last-page") { value.query_coverage!.matching_rows = 3; value.query_coverage!.offset = 1; }
  if (mode === "byte-limit") { value.query_coverage!.matching_rows = 100; value.next_cursor = "next"; }
  if (mode === "table-limit") recipe = { ...element, props: { ...element.props, row_limit: 1 } };
  if (mode === "stale") value.status = "stale";
  show(value, recipe);
  expect(screen.getByText("Pair grouping is incomplete. Original rows are shown.")).toBeTruthy();
  expect(screen.queryByRole("region", { name: "Pair match" })).toBeNull();
  expect(screen.getAllByRole("row")).toHaveLength(mode === "table-limit" ? 2 : 3);
});
it("refuses tampered counts, incompatible grouping fields and changed dataset binding", () => {
  const value = read(); value.query_coverage!.matching_rows = 1; const first = show(value);
  expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy(); first.unmount();
  const invalid = read(); invalid.fields[1]!.type = { kind: "number" };
  invalid.rows.forEach(row => { row.values.pair_key = 12; });
  expect(DatasetReadV1.safeParse(invalid).success).toBe(true);
  const second = show(invalid);
  expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy(); second.unmount();
  show({ ...read(), dataset_ref: { ...reference, digest: "b".repeat(64) } });
  expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
});
it("leaves ordinary table presentation untouched without the optional setting", () => {
  const { row_grouping, ...props } = element.props; show(read(), { ...element, props });
  expect(screen.getAllByRole("table")).toHaveLength(1);
  expect(screen.queryByText(/Pair grouping is incomplete/)).toBeNull();
  expect(screen.queryByRole("region", { name: "Pair match" })).toBeNull();
});
