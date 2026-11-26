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
const row = (key: string, pair: string | null, provider: string, remaining: number | null = 17) => ({ key,
  values: { account: key, pair_key: pair, provider, remaining, reset: "2026-11-12T12:00:00Z" } });
function read(rows = [row("a", "match", "Alpha"), row("b", "match", "Beta", 29)]): DatasetReadV1 {
  return { schema_version: 1, dataset_ref: reference, status: "ready", next_cursor: null,
    fields: ids.map(id => ({ id, label: id, type: { kind: id === "remaining" ? "number" : id === "reset" ? "instant" : "text" },
      nullable: id === "pair_key" || id === "remaining", sensitivity: "internal" })), rows,
    query_coverage: { kind: "complete_materialization", matching_rows: rows.length, offset: 0 },
    freshness: { fetched_at: "2026-11-12T12:00:00Z", synced_at: null, source_updated_at: null } };
}
const show = (value: unknown, recipe: unknown = element) => render(<DatasetElement element={recipe} read={value} preferences={resolveDisplayPreferences()} />);
const withMemberFilter = (member_filter: object) => ({ ...element,
  props: { ...element.props, row_grouping: { ...element.props.row_grouping, member_filter } } });

it.each([
  [{ field: "remaining", op: "lt", value: 25 }, 24, 25],
  [{ field: "remaining", op: "lte", value: 0.25 }, 0.25, 0.26],
  [{ field: "remaining", op: "is_null" }, null, 1],
  [{ field: "remaining", op: "gt", value: 90 }, 91, 90],
  [{ field: "remaining", op: "gte", value: 90 }, 90, 89],
])("matches either account for %j and keeps the complete verified pair", (filter, matching, other) => {
  show(read([
    row("healthy", "yes", "Alpha", 80), row("match", "yes", "Beta", matching),
    row("nonmatch", "no", "Alpha", other), row("healthy-2", "no", "Beta", 81),
    row("duplicate-a", "ambiguous", "Alpha", matching), row("duplicate-b", "ambiguous", "Alpha", 80),
    row("duplicate-c", "ambiguous", "Beta", 81),
  ]), withMemberFilter(filter));
  const pair = screen.getByRole("row", { name: /^Pair yes\b/ });
  expect(within(pair).getByText("healthy")).toBeTruthy();
  expect(within(pair).getByText("match")).toBeTruthy();
  expect(screen.getAllByRole("row")).toHaveLength(2);
  expect(screen.getByText(/1 of 2 verified pairs/)).toHaveTextContent("3 account rows without a verified pair are excluded.");
});

it("treats an omitted nullable value as unknown and preserves its partner", () => {
  const data = read([row("healthy", "yes", "Alpha", 80), row("unknown", "yes", "Beta"),
    row("zero", "no", "Alpha", 0), row("other", "no", "Beta", 10)]);
  Reflect.deleteProperty(data.rows[1]!.values, "remaining");
  show(data, withMemberFilter({ field: "remaining", op: "is_null" }));
  const pair = screen.getByRole("row", { name: /^Pair yes\b/ });
  expect(within(pair).getByText("healthy")).toBeTruthy();
  expect(within(pair).getByText("Unknown")).toBeTruthy();
  expect(screen.queryByRole("row", { name: /^Pair no\b/ })).toBeNull();
});

it.each(["empty", "no match"])("explains the filtered empty state for %s", mode => {
  const data = mode === "empty" ? { ...read([]), status: "empty" } : read();
  show(data, withMemberFilter({ field: "remaining", op: "lt", value: 0 }));
  expect(screen.getByText("No verified pairs match")).toBeTruthy();
  expect(screen.queryByText(/No data available/)).toBeNull();
});

it.each(["cursor", "row filter", "hidden field", "unrequested field", "text inequality", "unknown operator", "infinite threshold"])(
  "rejects a saved pair condition with %s", mode => {
    const recipe = withMemberFilter({ field: "remaining", op: "lt", value: 25 });
    if (mode === "cursor") Object.assign(recipe.query = { ...recipe.query }, { cursor: "next" });
    if (mode === "row filter") Object.assign(recipe.query = { ...recipe.query }, {
      filters: [{ field: "remaining", op: "lt", value: 25 }],
    });
    if (mode === "hidden field") recipe.props.columns = ids.filter(id => id !== "remaining");
    if (mode === "unrequested field") recipe.query = { ...recipe.query, fields: ids.filter(id => id !== "remaining") };
    if (mode === "text inequality") recipe.props.row_grouping.member_filter = { field: "account", op: "lt", value: 25 };
    if (mode === "unknown operator") recipe.props.row_grouping.member_filter = { field: "remaining", op: "eq", value: 25 };
    if (mode === "infinite threshold") recipe.props.row_grouping.member_filter = { field: "remaining", op: "lt", value: Infinity };
    show(read(), recipe);
    expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  },
);

it("leaves a saved pair condition unavailable on incomplete coverage", () => {
  const data = read();
  show({ ...data, next_cursor: "next", query_coverage: { ...data.query_coverage, matching_rows: 3 } },
    withMemberFilter({ field: "remaining", op: "lt", value: 25 }));
  expect(screen.getByText("Pair filtering requires complete dataset results.")).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
});
it("renders one paired row with separate provider cells, original metrics and reset values", () => {
  const value = read(), before = JSON.stringify(value); show(value);
  const pair = screen.getByRole("row", { name: /^Pair match\b/ });
  expect(within(pair).getByRole("cell", { name: "Pair match", exact: true })).toBeTruthy();
  expect(within(pair).getByText("17")).toBeTruthy(); expect(within(pair).getByText("29")).toBeTruthy();
  expect(within(pair).getAllByText(/Nov 12, 2026/)).toHaveLength(2);
  expect(screen.getAllByRole("table")).toHaveLength(1);
  expect(screen.getAllByRole("row")).toHaveLength(2);
  expect(screen.getByRole("columnheader", { name: "Alpha remaining" })).toBeTruthy();
  expect(screen.getByRole("columnheader", { name: "Beta remaining" })).toBeTruthy();
  expect(screen.queryByText("46")).toBeNull();
  expect(JSON.stringify(value)).toBe(before);
});
it("keeps a third provider separate even with the paired key, with all unpaired rows last", () => {
  show(read([row("third", "match", "Gamma"), row("a", "match", "Alpha"), row("b", "match", "Beta"), row("alone", "", "Alpha")]));
  const rendered = screen.getAllByRole("row");
  expect(rendered).toHaveLength(4);
  expect(rendered[1]!.textContent).toContain("Pair match");
  expect(rendered[2]!.textContent).toContain("Unpaired Gamma");
  expect(rendered[3]!.textContent).toContain("Unpaired Alpha");
  expect(within(rendered[2]!).getByText("third")).toBeTruthy();
  expect(within(rendered[3]!).getByText("alone")).toBeTruthy();
  expect(within(rendered[3]!).getByLabelText("Beta remaining unavailable")).toHaveTextContent("Unavailable");
  expect(within(rendered[2]!).getByLabelText("Alpha remaining unavailable")).toHaveTextContent("Unavailable");
  expect(within(rendered[2]!).getByLabelText("Beta reset unavailable")).toHaveTextContent("Unavailable");

});
it.each(["", " ", null])("does not pair blank/missing key %s", key => {
  show(read([row("a", key, "Alpha"), row("b", key, "Beta")]));
  expect(screen.queryByRole("cell", { name: /^Pair / })).toBeNull();
  expect(screen.getAllByRole("row")).toHaveLength(3);
});
it("keeps duplicates, missing partners and distinct key case/space separate", () => {
  show(read([row("a1", "duplicate", "Alpha"), row("a2", "duplicate", "Alpha"), row("b", "duplicate", "Beta"),
    row("case", "X", "Alpha"), row("case2", "x", "Beta"), row("space", " x", "Alpha")]));
  expect(screen.queryByRole("cell", { name: /^Pair / })).toBeNull();
  expect(screen.getAllByRole("row")).toHaveLength(7);
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
  expect(screen.queryByRole("row", { name: /^Pair match\b/ })).toBeNull();
  expect(screen.getAllByRole("row")).toHaveLength(mode === "table-limit" ? 2 : 3);
});
it.each(["absent", "table-limit", "stale"])("hides filtered rows for %s coverage", mode => {
  const data = read();
  const recipe = withMemberFilter({ field: "remaining", op: "lt", value: 25 });
  if (mode === "absent") delete data.query_coverage;
  if (mode === "table-limit") recipe.props.row_limit = 1;
  if (mode === "stale") data.status = "stale";
  show(data, recipe);
  expect(screen.getByText("Pair filtering requires complete dataset results.")).toBeTruthy();
  expect(screen.queryByRole("table")).toBeNull();
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
  expect(screen.queryByRole("row", { name: /^Pair match\b/ })).toBeNull();
});

it("renders two pairs as two rows and two independent unmatched rows without changing input", () => {
  const value = read([row("main-a", "main", "Alpha", 80), row("main-b", "main", "Beta", 65),
    row("second-a", "second", "Alpha", 45), row("second-b", "second", "Beta", 30),
    row("alone", "", "Alpha", 20), row("kimi", "", "Gamma", 90)]);
  const before = JSON.stringify(value); show(value);
  const rendered = screen.getAllByRole("row");
  expect(rendered).toHaveLength(5);
  expect(rendered.slice(1, 3).every(row => row.textContent!.startsWith("Pair "))).toBe(true);
  expect(within(rendered[1]!).getByText("80")).toBeTruthy();
  expect(within(rendered[1]!).getByText("65")).toBeTruthy();
  expect(within(rendered[2]!).getByText("45")).toBeTruthy();
  expect(within(rendered[2]!).getByText("30")).toBeTruthy();
  expect(within(rendered[3]!).getByText("20")).toBeTruthy();
  expect(within(rendered[3]!).getByLabelText("Beta remaining unavailable")).toHaveTextContent("Unavailable");
  expect(within(rendered[4]!).getByText("kimi")).toBeTruthy();
  expect(within(rendered[4]!).getByText("90")).toBeTruthy();
  expect(screen.getByRole("region", { name: "Capacity data: provider pairs" })).toHaveAttribute("tabindex", "0");
  expect(JSON.stringify(value)).toBe(before);
});
