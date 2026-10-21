"use client";

import type { CSSProperties, ReactNode } from "react";
import { LEAF_TOKENS } from "../tokens";

export interface DataTableColumn<Row> {
  key: string;
  header: string;
  numeric?: boolean;
  cell: (row: Row) => ReactNode;
}

export interface DataTableCoreProps<Row> {
  columns: readonly DataTableColumn<Row>[];
  rows: readonly Row[];
  getRowKey: (row: Row) => string;
  /** Visible row budget; all supplied rows remain available by scrolling. */
  maxRows?: number;
  label?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  className?: string;
  style?: CSSProperties;
}

const colors = LEAF_TOKENS.color.light;

/** A bounded viewport, without selection, sorting, fetching or host CSS. */
export function DataTableCore<Row>({
  columns, rows, getRowKey, maxRows = 12, label = "Dataset data",
  emptyTitle = "No data available", emptyDescription = "There are no rows to display.",
  className, style,
}: DataTableCoreProps<Row>) {
  const budget = Number.isFinite(maxRows) ? Math.max(1, Math.min(1000, Math.floor(maxRows))) : 12;
  return (
    <div role="region" aria-label={label} tabIndex={0} className={className}
      style={{ minWidth: 0, maxWidth: "100%", overflow: "auto", maxHeight: 44 + budget * 36,
        color: `var(--leaf-foreground, var(--foreground, ${colors.foreground}))`,
        background: `var(--leaf-card, var(--card, ${colors.card}))`, ...style }}>
      <table data-slot="table" aria-label={label}
        style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
        <thead><tr>{columns.map((column) => (
          <th key={column.key} scope="col" style={{ padding: "8px 12px", textAlign: column.numeric ? "right" : "left",
            overflowWrap: "anywhere", borderBottom: `1px solid var(--leaf-border, var(--border, ${colors.border}))` }}>
            {column.header}
          </th>
        ))}</tr></thead>
        <tbody>{rows.map((row) => (
          <tr key={getRowKey(row)}>{columns.map((column) => (
            <td key={column.key} style={{ padding: "8px 12px", verticalAlign: "top", overflowWrap: "anywhere",
              textAlign: column.numeric ? "right" : "left", fontVariantNumeric: column.numeric ? "tabular-nums" : undefined,
              borderBottom: `1px solid var(--leaf-border, var(--border, ${colors.border}))` }}>
              {column.cell(row)}
            </td>
          ))}</tr>
        ))}</tbody>
      </table>
      {rows.length === 0 && <div role="status" style={{ padding: 12 }}><p>{emptyTitle}</p><p>{emptyDescription}</p></div>}
    </div>
  );
}
