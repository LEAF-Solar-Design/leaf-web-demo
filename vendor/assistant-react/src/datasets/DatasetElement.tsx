"use client";

import { DatasetElementV1, DatasetReadV1, type DatasetFieldV1 } from "../../../assistant-contract/src/integration";
import { LEAF_TOKENS, target } from "../tokens";
import { DataTableCore, type DataTableColumn } from "./DataTableCore";
import { LineChartCore, projectDatasetLine, formatDatasetAxisValue } from "./LineChartCore";
import { formatDatasetValue, resolveDisplayPreferences, type DisplayPreferences } from "./formatting";

export interface DatasetElementProps {
  element: unknown;
  read: unknown;
  preferences: DisplayPreferences;
  onRetry?: () => void;
  onLoadMore?: (cursor: string) => void;
  isLoading?: boolean;
}

const messages = {
  loading: "Loading dataset…",
  empty: "No data available.",
  forbidden: "You do not have permission to view this dataset.",
  unavailable: "This dataset is unavailable.",
  failed: "The dataset could not be loaded.",
  invalid: "Invalid dataset or binding.",
  stale: "Stale data: the last successful data is shown.",
  partial: "Partial data: some results are not available.",
} as const;
const numericKinds = new Set(["number", "count", "ratio", "money", "duration"]);
const xKinds = new Set(["instant", "date", "number", "count"]);
type Row = DatasetReadV1["rows"][number];
const colors = LEAF_TOKENS.color.light;
const buttonStyle = {
  ...target, padding: "8px 16px", font: "inherit", borderRadius: "var(--leaf-radius, 0.5rem)",
  color: `var(--leaf-foreground, var(--foreground, ${colors.foreground}))`,
  background: `var(--leaf-card, var(--card, ${colors.card}))`,
  border: `1px solid var(--leaf-border, var(--border, ${colors.border}))`,
};
const rootStyle = {
  minWidth: 0, maxWidth: "100%", display: "grid", gap: 12,
  color: `var(--leaf-foreground, var(--foreground, ${colors.foreground}))`,
  fontFamily: `var(--leaf-font-sans, ${LEAF_TOKENS.font.sans})`,
};

function Freshness({ freshness, preferences }: {
  freshness: DatasetReadV1["freshness"];
  preferences: DisplayPreferences;
}) {
  const field: DatasetFieldV1 = {
    id: "timestamp", label: "Timestamp", type: { kind: "instant" }, nullable: false, sensitivity: "public",
  };
  const timestamps = [
    ["Source updated", freshness.source_updated_at],
    ["Successfully synchronized", freshness.synced_at],
    ["Fetched", freshness.fetched_at],
  ] as const;
  return (
    <dl style={{ display: "flex", flexWrap: "wrap", gap: "4px 16px", margin: 0, fontSize: 13 }}>
      {timestamps.map(([label, value]) => value && (
        <div key={label}><dt>{label}</dt><dd style={{ margin: 0 }}>
          <time dateTime={value}>{formatDatasetValue(field, value, preferences)}</time>
        </dd></div>
      ))}
    </dl>
  );
}

/** Validates bindings and renders supplied data only. Hosts own all I/O and authorization. */
export function DatasetElement({
  element: rawElement, read: rawRead, preferences, onRetry, onLoadMore, isLoading = false,
}: DatasetElementProps) {
  const retry = onRetry ? (
    <button type="button" style={buttonStyle} disabled={isLoading}
      onClick={() => { if (!isLoading) onRetry(); }}>Retry</button>
  ) : null;
  const state = (name: keyof typeof messages) => (
    <div style={rootStyle} aria-busy={isLoading}>
      <p role="status" aria-live="polite">{messages[name]}</p>
      {name !== "loading" && retry}
    </div>
  );
  const parsedElement = DatasetElementV1.safeParse(rawElement);
  if (!parsedElement.success) return state("invalid");
  if (isLoading && rawRead == null) return state("loading");
  const parsedRead = DatasetReadV1.safeParse(rawRead);
  if (!parsedRead.success) return state("invalid");
  const element = parsedElement.data;
  const read = parsedRead.data;
  if ((["id", "version", "digest"] as const).some((key) =>
    element.dataset_ref[key] !== read.dataset_ref[key])) return state("invalid");
  const fields = new Map(read.fields.map((field) => [field.id, field]));
  const ids = element.kind === "dataset.table"
    ? element.props.columns : [...new Set([element.props.x_field, element.props.y_field])];
  const requested = new Set(element.query.fields);
  if (element.query.fields.some((id) => !fields.has(id)) ||
      ids.some((id) => !requested.has(id) || !fields.has(id))) return state("invalid");
  if (element.kind === "dataset.line" &&
      (!xKinds.has(fields.get(element.props.x_field)!.type.kind) ||
       !numericKinds.has(fields.get(element.props.y_field)!.type.kind))) return state("invalid");
  let prefs: DisplayPreferences;
  try { prefs = resolveDisplayPreferences(preferences); }
  catch { return state("invalid"); }
  if (read.status === "forbidden" || read.status === "invalid" ||
      read.status === "unavailable" || read.status === "failed") return (
    <section aria-label={element.title} style={rootStyle}>
      {state(read.status)}
      <Freshness freshness={read.freshness} preferences={prefs} />
    </section>
  );
  // Semantic formatting can reject currency codes that Intl would otherwise invent digits for.
  if (read.rows.some((row) => read.fields.some((field) =>
    field.type.kind === "money" && row.values[field.id] != null &&
    formatDatasetValue(field, row.values[field.id], prefs) === "Invalid"))) return state("invalid");
  const limit = element.kind === "dataset.table"
    ? Math.min(element.query.limit, element.props.row_limit) : element.query.limit;
  const rows = read.rows.slice(0, limit);
  const columns: DataTableColumn<Row>[] = ids.map((id) => {
    const field = fields.get(id)!;
    return {
      key: id, header: field.label, numeric: numericKinds.has(field.type.kind),
      cell: (row) => formatDatasetValue(field, row.values[id] ?? null, prefs),
    };
  });
  const table = <DataTableCore columns={columns} rows={rows} getRowKey={(row) => row.key}
    maxRows={12} label={`${element.title} data`} />;
  const xField = element.kind === "dataset.line" ? fields.get(element.props.x_field)! : null;
  const yField = element.kind === "dataset.line" ? fields.get(element.props.y_field)! : null;
  const chartRows = xField && yField ? projectDatasetLine(rows, xField, yField) : [];
  return (
    <section aria-label={element.title} aria-busy={isLoading} style={rootStyle}>
      <h2 style={{ margin: 0, fontSize: 16, overflowWrap: "anywhere" }}>{element.title}</h2>
      <div role="status" aria-live="polite">
        {isLoading && <p>{messages.loading}</p>}
        {(read.status === "stale" || read.status === "partial") && <p>{messages[read.status]}</p>}
        {rows.length === 0 && <p>{messages.empty}</p>}
      </div>
      {element.kind === "dataset.table" ? table : rows.length > 0 ? (
        <>
          <LineChartCore hideCard data={chartRows} xKey="x" yKey="y" connectNulls={false}
            xAxisType="number" xAxisDomain={["dataMin", "dataMax"]}
            xAxisAllowDecimals={xField!.type.kind !== "count"}
            yAxisAllowDecimals={yField!.type.kind !== "count" &&
              !(yField!.type.kind === "money" && yField!.type.scale === "minor")}
            xLabel={xField!.label} valueLabel={yField!.label}
            xFormatter={(value) => formatDatasetAxisValue(xField!, value, prefs)}
            yFormatter={(value) => formatDatasetAxisValue(yField!, value, prefs)}
            accessibilityLabel={element.title}
            accessibilityDescription="Missing values are gaps. Open View data table for the same values in a table." />
          <details>
            <summary style={{ ...target, cursor: "pointer", padding: "8px 0", boxSizing: "border-box" }}>
              View data table
            </summary>
            {table}
          </details>
        </>
      ) : null}
      <Freshness freshness={read.freshness} preferences={prefs} />
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {(read.status === "stale" || read.status === "partial") && retry}
        {read.next_cursor !== null && onLoadMore && (
          <button type="button" style={buttonStyle} disabled={isLoading}
            onClick={() => { if (!isLoading) onLoadMore(read.next_cursor!); }}>Load more</button>
        )}
      </div>
    </section>
  );
}
