import type { DatasetReadV1, DatasetRowGroupingV1 } from "../../../assistant-contract/src/integration";
import { DataTableCore, type DataTableColumn } from "./DataTableCore";

type Row = DatasetReadV1["rows"][number];
export function ProviderPairTable({ rows, columns, grouping, complete, label }: {
  rows: readonly Row[]; columns: readonly DataTableColumn<Row>[];
  grouping: DatasetRowGroupingV1; complete: boolean; label: string;
}) {
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
  const unpaired = rows.filter(row => !paired.has(row));
  return <div>
    {groups.map(group => <section key={group.key} aria-label={`Pair ${group.key}`}>
      <h3>Pair {group.key}</h3>
      <p>{grouping.providers[0]} and {grouping.providers[1]}</p>
      <DataTableCore rows={group.rows} columns={columns} getRowKey={row => row.key} label={`${label}: pair ${group.key}`} />
    </section>)}
    {unpaired.length > 0 && <section aria-label="Unpaired accounts">
      <h3>Unpaired accounts</h3>
      <DataTableCore rows={unpaired} columns={columns} getRowKey={row => row.key} label={`${label}: unpaired accounts`} />
    </section>}
  </div>;
}
