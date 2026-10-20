"use client";

import { useId, type CSSProperties } from "react";
import {
  LineChart as RechartsLineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import type { DatasetFieldV1, DatasetReadV1 } from "../../../assistant-contract/src/integration";
import { LEAF_TOKENS, srOnly } from "../tokens";
import { formatDatasetValue, type DisplayPreferences } from "./formatting";

export interface LineChartCoreProps {
  title?: string;
  data: object[];
  xKey: string;
  yKey: string;
  color?: string;
  height?: number;
  yAxisDomain?: [number, number];
  referenceLine?: { y: number; label: string; color?: string };
  hideCard?: boolean;
  xFormatter?: (value: unknown) => string;
  yFormatter?: (value: unknown) => string;
  valueLabel?: string;
  xLabel?: string;
  xAxisType?: "category" | "number";
  xAxisDomain?: [number | "dataMin" | "dataMax", number | "dataMin" | "dataMax"];
  xAxisTicks?: number[];
  xAxisAllowDecimals?: boolean;
  yAxisAllowDecimals?: boolean;
  accessibilityLabel?: string;
  accessibilityDescription?: string;
  connectNulls?: boolean;
  className?: string;
  style?: CSSProperties;
}

export type LineChartProps = LineChartCoreProps;
export interface DatasetChartPoint { x: number; y: number | null }

/** Project validated dataset rows; never reorder the authoritative read or its table. */
export function projectDatasetLine(
  rows: readonly DatasetReadV1["rows"][number][], xField: DatasetFieldV1, yField: DatasetFieldV1,
): DatasetChartPoint[] {
  return rows.flatMap((row) => {
    const raw = row.values[xField.id];
    if (raw == null) return [];
    const fraction = xField.type.kind === "instant" ? /\.(\d+)Z$/.exec(String(raw)) : null;
    // Date.parse truncates submillisecond digits; add them at Number precision instead.
    const x = fraction
      ? Date.parse(`${String(raw).slice(0, fraction.index)}Z`) + Number(`0.${fraction[1]}`) * 1000
      : xField.type.kind === "instant" || xField.type.kind === "date"
        ? Date.parse(String(raw)) : Number(raw);
    const y = row.values[yField.id];
    return Number.isFinite(x) ? [{ x, y: typeof y === "number" ? y : null }] : [];
  }).sort((a, b) => a.x - b.x);
}

/** Recharts may generate ticks beyond its data domain or outside the Date range. */
export function formatDatasetAxisValue(field: DatasetFieldV1, value: unknown, preferences: DisplayPreferences): string {
  let canonical = value === null || typeof value === "string" || typeof value === "number" || typeof value === "boolean"
    ? value : null;
  if ((field.type.kind === "instant" || field.type.kind === "date") && typeof value === "number") {
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "Invalid";
    const instant = date.toISOString();
    // The canonical contract supports four-digit years only.
    if (!/^\d{4}-/.test(instant)) return "Invalid";
    canonical = field.type.kind === "date" ? instant.slice(0, 10) : instant;
  }
  return formatDatasetValue(field, canonical, preferences);
}

const colors = LEAF_TOKENS.color.light;
const ink = `var(--leaf-muted-foreground, var(--muted-foreground, ${colors["muted-foreground"]}))`;

export function LineChartCore({
  title, data, xKey, yKey, color = `var(--leaf-chart-1, var(--chart-1, ${colors["chart-1"]}))`,
  height = 300, yAxisDomain, referenceLine, hideCard = false,
  xFormatter, yFormatter, valueLabel, xLabel, xAxisType = "category", xAxisDomain, xAxisTicks,
  xAxisAllowDecimals, yAxisAllowDecimals, accessibilityLabel, accessibilityDescription,
  connectNulls = false, className, style,
}: LineChartCoreProps) {
  const descriptionId = useId();
  return (
    <div role="group" aria-label={accessibilityLabel ?? title}
      aria-describedby={accessibilityDescription ? descriptionId : undefined} className={className}
      style={{ minWidth: 0, width: "100%", maxWidth: "100%", boxSizing: "border-box",
        ...(!hideCard ? { padding: 12, border: `1px solid var(--leaf-border, var(--border, ${colors.border}))`,
          borderRadius: "var(--leaf-radius, 0.5rem)" } : {}), ...style }}>
      {title && !hideCard && <h2 style={{ fontSize: 16 }}>{title}</h2>}
      {accessibilityDescription && <p id={descriptionId} style={srOnly}>{accessibilityDescription}</p>}
      <ResponsiveContainer width="100%" height={height} minWidth={0}>
        <RechartsLineChart data={data} accessibilityLayer margin={{ top: 16, right: 40, bottom: 16, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke={`var(--leaf-border, var(--border, ${colors.border}))`} />
          <XAxis dataKey={xKey} type={xAxisType} domain={xAxisDomain} ticks={xAxisTicks} allowDecimals={xAxisAllowDecimals}
            tick={{ fill: ink, fontSize: 12 }}
            label={xLabel ? { value: xLabel, position: "insideBottom", offset: -5, fill: ink } : undefined}
            tickFormatter={xFormatter ?? ((value) => typeof value === "string" && value.includes("-")
              ? value.split("-").slice(1).join("/") : String(value))} />
          <YAxis domain={yAxisDomain} allowDecimals={yAxisAllowDecimals} tickFormatter={yFormatter}
            tick={{ fill: ink, fontSize: 12 }} />
          <Tooltip labelFormatter={xFormatter}
            contentStyle={{ backgroundColor: `var(--leaf-card, var(--card, ${colors.card}))`,
              color: `var(--leaf-foreground, var(--foreground, ${colors.foreground}))`,
              border: `1px solid var(--leaf-border, var(--border, ${colors.border}))`, borderRadius: "var(--leaf-radius, 0.5rem)" }}
            formatter={(value) => [yFormatter ? yFormatter(value) : typeof value === "number" ? value.toFixed(1) : String(value ?? ""), valueLabel ?? yKey]} />
          {referenceLine && <ReferenceLine y={referenceLine.y}
            stroke={referenceLine.color ?? `var(--leaf-chart-2, var(--chart-2, ${colors["chart-2"]}))`}
            strokeDasharray="3 3" label={{ value: referenceLine.label, fill: ink, fontSize: 12 }} />}
          <Line type="monotone" dataKey={yKey} stroke={color} strokeWidth={2} dot={{ r: 3, fill: color }}
            activeDot={{ r: 4 }} connectNulls={connectNulls} isAnimationActive={false} />
        </RechartsLineChart>
      </ResponsiveContainer>
    </div>
  );
}
