import type { DatasetReadV1, DatasetRowGroupingV1 } from "../../../assistant-contract/src/integration";
import { DataTableCore, type DataTableColumn } from "./DataTableCore";

type Row = DatasetReadV1["rows"][number];
type MemberFilter = NonNullable<DatasetRowGroupingV1["member_filter"]>;

function matchesMember(row: Row, filter: MemberFilter): boolean {
  const value = row.values[filter.field];
  if (filter.op === "is_null") return value == null;
  if (typeof value !== "number") return false;
  switch (filter.op) {
    case "lt": return value < filter.value;
    case "lte": return value <= filter.value;
    case "gt": return value > filter.value;
    case "gte": return value >= filter.value;
  }
}
export function ProviderPairTable({ rows, columns, grouping, complete, label }: {
  rows: readonly Row[]; columns: readonly DataTableColumn<Row>[];
  grouping: DatasetRowGroupingV1; complete: boolean; label: string;
}) {
  const filter = grouping.member_filter;
  if (!complete && filter) return <p role="status">Pair filtering requires complete dataset results.</p>;
  if (!complete) return <>
    <p role="status">Pair grouping is incomplete. Original rows are shown.</p>
    <DataTableCore rows={rows} columns={columns} getRowKey={row => row.key} label={label} />
  </>;
  const candidates = new Map<string, Row[]>();
  for (const row of rows) {
    const key = row.values[grouping.pair_key_field];
    const provider = row.values[grouping.provider_field];
    if (typeof key !== "string" || !key.trim().length || typeof provider !== "string" ||
        !grouping.providers.includes(provider)) continue;
    const bucket = candidates.get(key) ?? [];
    bucket.push(row); candidates.set(key, bucket);
  }
  const paired = new Set<Row>();
  const groups: { key: string; rows: Row[] }[] = [];
  for (const [key, bucket] of candidates) {
    if (bucket.length !== 2) continue;
    const ordered = grouping.providers.map(provider => bucket.filter(row => row.values[grouping.provider_field] === provider));
    if (ordered.some(matches => matches.length !== 1)) continue;
    const pair = ordered.map(matches => matches[0]!);
    pair.forEach(row => paired.add(row)); groups.push({ key, rows: pair });
  }
  const unpairedCount = rows.length - paired.size;
  const unpaired = filter ? [] : rows.filter(row => !paired.has(row));
  type Presented = { key: string; title: string; slots: readonly (Row | undefined)[] };
  const eligibleGroups = filter ? groups.filter(group => group.rows.some(row => matchesMember(row, filter))) : groups;
  const presented: Presented[] = eligibleGroups.map(group => ({
    key: `pair:${JSON.stringify([group.key, ...group.rows.map(row => row.key)])}`,
    title: `Pair ${group.key}`, slots: group.rows,
  }));
  let hasOther = false;
  for (const row of unpaired) {
    const provider = row.values[grouping.provider_field];
    const index = typeof provider === "string" ? grouping.providers.indexOf(provider) : -1;
    const slots: (Row | undefined)[] = [];
    slots[index < 0 ? 2 : index] = row;
    if (index < 0) hasOther = true;
    const key = row.values[grouping.pair_key_field];
    presented.push({ key: `unpaired:${row.key}`, slots,
      title: `Unpaired ${typeof provider === "string" && provider ? provider : "account"}${typeof key === "string" && key.trim() ? ` (key: ${key})` : ""}` });
  }
  // Provider identity is in the headers; verified pair identity is in the first cell.
  // Other providers retain separate rows, never a combined quota or a fabricated partner.
  const metrics = columns.filter(column => column.key !== grouping.provider_field && column.key !== grouping.pair_key_field);
  const visible = metrics.length ? metrics : columns;
  const providers = hasOther ? [...grouping.providers, "Other"] : grouping.providers;
  const pairedColumns: DataTableColumn<Presented>[] = [{ key: "pair", header: "Pair / account",
    cell: row => <span style={{ display: "inline-block", minWidth: 96 }}>{row.title}</span> }];
  providers.forEach((provider, index) => visible.forEach(column => {
    pairedColumns.push({ key: `${index}:${column.key}`, header: `${provider} ${column.header}`, numeric: column.numeric,
      cell: row => <span style={{ display: "inline-block", minWidth: column.numeric ? 56 : 112 }}>
        {row.slots[index] ? column.cell(row.slots[index]!) : <span aria-label={`${provider} ${column.header} unavailable`}>Unavailable</span>}
      </span> });
  }));
  return <>
    {filter
      ? <p role="status">
        {eligibleGroups.length} of {groups.length} verified pairs match the saved condition.
        {unpairedCount > 0 && ` ${unpairedCount} account rows without a verified pair are excluded.`}
      </p>
      : <p>Each verified pair shares one row. Provider values stay separate. Unpaired accounts follow; missing providers are unavailable.</p>}
    <DataTableCore rows={presented} columns={pairedColumns} getRowKey={row => row.key} label={`${label}: provider pairs`}
      emptyTitle={filter ? "No verified pairs match" : undefined}
      emptyDescription={filter ? "The saved condition must match at least one account in a verified pair." : undefined} />
  </>;
}
