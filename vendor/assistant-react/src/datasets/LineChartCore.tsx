"use client";

import { useEffect, useId, useRef, useState, type CSSProperties } from "react";
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

/** Generated ticks may lie beyond the data domain or outside the Date range. */
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

function extent(values: number[]): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const value of values) { min = Math.min(min, value); max = Math.max(max, value); }
  if (!Number.isFinite(min)) return [0, 1];
  return min === max ? [min - (Math.abs(min) * 0.05 || 1), max + (Math.abs(max) * 0.05 || 1)] : [min, max];
}

function niceStep(span: number): number {
  const power = 10 ** Math.floor(Math.log10(span / 4));
  const fraction = span / 4 / power;
  return (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power;
}

function ticksFor(domain: [number, number], decimals: boolean | undefined, format: (value: number) => string): number[] {
  let step = niceStep(domain[1] - domain[0]);
  if (decimals === false) step = Math.max(1, step);
  const ticks: number[] = [];
  const labels = new Set<string>();
  const first = Math.ceil(domain[0] / step);
  const last = Math.floor(domain[1] / step);
  for (let offset = 0; offset < 6 && first + offset <= last && ticks.length < 5; offset++) {
    const value = Number(((first + offset) * step).toPrecision(16));
    if (value < domain[0] || value > domain[1]) continue;
    const label = format(value);
    if (!labels.has(label)) { ticks.push(value); labels.add(label); }
  }
  return ticks;
}

type PlotPoint = { x: number; y: number; rawX: unknown; rawY: number };

/** Fritsch-Carlson tangents keep each cubic within its neighbouring observations. */
function monotonePath(points: PlotPoint[]): string {
  if (points.length < 2) return "";
  const slopes = points.slice(1).map((point, i) => {
    const dx = point.x - points[i].x;
    return dx === 0 ? 0 : (point.y - points[i].y) / dx;
  });
  const tangents = points.map((_, i) => i === 0 ? slopes[0] : i === points.length - 1
    ? slopes[i - 1] : (slopes[i - 1] + slopes[i]) / 2);
  for (let i = 0; i < slopes.length; i++) {
    const slope = slopes[i];
    if (slope === 0) { tangents[i] = 0; tangents[i + 1] = 0; continue; }
    if (tangents[i] / slope < 0) tangents[i] = 0;
    if (tangents[i + 1] / slope < 0) tangents[i + 1] = 0;
    const length = Math.hypot(tangents[i] / slope, tangents[i + 1] / slope);
    if (length > 3) {
      tangents[i] *= 3 / length;
      tangents[i + 1] *= 3 / length;
    }
  }
  return `M${points[0].x},${points[0].y}` + points.slice(1).map((point, i) => {
    const previous = points[i];
    const dx = (point.x - previous.x) / 3;
    return `C${previous.x + dx},${previous.y + dx * tangents[i]} ${point.x - dx},${point.y - dx * tangents[i + 1]} ${point.x},${point.y}`;
  }).join("");
}

export function LineChartCore({
  title, data, xKey, yKey, color = `var(--leaf-chart-1, var(--chart-1, ${colors["chart-1"]}))`,
  height = 300, yAxisDomain, referenceLine, hideCard = false,
  xFormatter, yFormatter, valueLabel, xLabel, xAxisType = "category", xAxisDomain, xAxisTicks,
  xAxisAllowDecimals, yAxisAllowDecimals, accessibilityLabel, accessibilityDescription,
  connectNulls = false, className, style,
}: LineChartCoreProps) {
  const descriptionId = useId();
  const clipId = useId();
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  useEffect(() => {
    if (typeof ResizeObserver === "undefined" || !container.current) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry && entry.contentRect.width > 0) setWidth(entry.contentRect.width);
    });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const formatX = xFormatter ?? ((value: unknown) => typeof value === "string" && value.includes("-")
    ? value.split("-").slice(1).join("/") : String(value));
  const formatY = yFormatter ?? ((value: unknown) => String(value));
  const rows = data.map((row) => {
    const values = row as Record<string, unknown>;
    return { x: values[xKey], y: values[yKey] };
  });
  const numericXs = rows.flatMap(row => row.x != null && Number.isFinite(Number(row.x)) ? [Number(row.x)] : []);
  const xExtent = extent(numericXs);
  const resolveX = (bound: number | "dataMin" | "dataMax") =>
    bound === "dataMin" ? xExtent[0] : bound === "dataMax" ? xExtent[1] : bound;
  const xDomain = xAxisDomain ? xAxisDomain.map(resolveX) as [number, number] : xExtent;
  if (xDomain[0] === xDomain[1]) { xDomain[0] -= 1; xDomain[1] += 1; }
  const yValues = rows.flatMap(row => typeof row.y === "number" && Number.isFinite(row.y) ? [row.y] : []);
  const yDomain: [number, number] = yAxisDomain ? [...yAxisDomain] :
    [yValues.reduce((min, value) => Math.min(min, value), 0),
      yValues.length ? yValues.reduce((max, value) => Math.max(max, value), -Infinity) : 1];
  if (yDomain[0] === yDomain[1]) {
    if (yAxisDomain || yDomain[0] !== 0) yDomain[0] -= 1;
    yDomain[1] += 1;
  }
  const yTicks = yAxisDomain ? ticksFor(yDomain, yAxisAllowDecimals, formatY) :
    Array.from({ length: 5 }, (_, index) => yDomain[0] + (yDomain[1] - yDomain[0]) * index / 4)
      .filter(value => yAxisAllowDecimals !== false || Number.isInteger(value))
      .filter((value, index, ticks) => ticks.findIndex(other => formatY(other) === formatY(value)) === index);
  const left = Math.min(width / 2, Math.max(40, ...yTicks.map(value => formatY(value).length * 7 + 12)));
  const right = Math.max(left + 1, width - 40);
  const top = 16;
  const bottom = Math.max(top + 1, height - 16 - (xLabel ? 40 : 24));
  const scaleX = (value: number) => left + (value - xDomain[0]) / (xDomain[1] - xDomain[0]) * (right - left);
  const scaleY = (value: number) => bottom - (value - yDomain[0]) / (yDomain[1] - yDomain[0]) * (bottom - top);
  const categoryX = (index: number) => rows.length <= 1 ? (left + right) / 2 : left + index / (rows.length - 1) * (right - left);
  const xTicks = xAxisTicks ? xAxisTicks.map(value => ({ value, position: xAxisType === "number" ? scaleX(value) : categoryX(rows.findIndex(row => row.x === value)) })) :
    xAxisType === "number" ? ticksFor(xDomain, xAxisAllowDecimals, formatX).map(value => ({ value, position: scaleX(value) })) :
      rows.flatMap((row, index) => index % Math.max(1, Math.ceil((rows.length - 1) / 4)) === 0
        ? [{ value: row.x, position: categoryX(index) }] : []).filter((tick, index, ticks) =>
        ticks.findIndex(other => formatX(other.value) === formatX(tick.value)) === index);
  const segments: PlotPoint[][] = [];
  let segment: PlotPoint[] = [];
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    if (typeof row.y !== "number" || !Number.isFinite(row.y) ||
        (xAxisType === "number" && (row.x == null || !Number.isFinite(Number(row.x))))) {
      if (!connectNulls && segment.length) { segments.push(segment); segment = []; }
      continue;
    }
    segment.push({ x: xAxisType === "number" ? scaleX(Number(row.x)) : categoryX(i),
      y: scaleY(row.y), rawX: row.x, rawY: row.y });
  }
  if (segment.length) segments.push(segment);
  const points = segments.flat();
  const active = activeIndex === null ? undefined : points[activeIndex];
  const tooltipValue = active ? yFormatter ? yFormatter(active.rawY) : active.rawY.toFixed(1) : "";
  const tooltipText = active ? `${xFormatter ? xFormatter(active.rawX) : String(active.rawX ?? "")} ${valueLabel ?? yKey}: ${tooltipValue}` : "";
  const tooltipLeft = active ? Math.max(0, Math.min(active.x + 12, width - 200)) : 0;
  const tooltipTop = active ? Math.max(0, Math.min(active.y + 12, height - 60)) : 0;
  return (
    <div role="group" aria-label={accessibilityLabel ?? title}
      aria-describedby={accessibilityDescription ? descriptionId : undefined} className={className}
      style={{ minWidth: 0, width: "100%", maxWidth: "100%", boxSizing: "border-box",
        ...(!hideCard ? { padding: 12, border: `1px solid var(--leaf-border, var(--border, ${colors.border}))`,
          borderRadius: "var(--leaf-radius, 0.5rem)" } : {}), ...style }}>
      {title && !hideCard && <h2 style={{ fontSize: 16 }}>{title}</h2>}
      {accessibilityDescription && <p id={descriptionId} style={srOnly}>{accessibilityDescription}</p>}
      <div ref={container} style={{ position: "relative", width: "100%", height }}>
        <svg width="100%" height={height} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="application"
          aria-label={accessibilityLabel ?? title ?? "Line chart"} tabIndex={0}
          onFocus={() => setActiveIndex(points.length ? 0 : null)} onBlur={() => setActiveIndex(null)}
          onKeyDown={(event) => {
            if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key) || !points.length) return;
            event.preventDefault();
            setActiveIndex(index => event.key === "Home" ? 0 : event.key === "End" ? points.length - 1 :
              Math.max(0, Math.min(points.length - 1, (index ?? 0) + (event.key === "ArrowRight" ? 1 : -1))));
          }}
          onPointerMove={(event) => {
            const bounds = event.currentTarget.getBoundingClientRect();
            const x = (event.clientX - bounds.left) * width / (bounds.width || width);
            const y = (event.clientY - bounds.top) * height / (bounds.height || height);
            if (x < left || x > right || y < top || y > bottom) { setActiveIndex(null); return; }
            let nearest: number | null = null;
            points.forEach((point, index) => {
              if (nearest === null || Math.abs(point.x - x) < Math.abs(points[nearest].x - x)) nearest = index;
            });
            setActiveIndex(nearest);
          }} onPointerLeave={() => setActiveIndex(null)}>
          <defs><clipPath id={clipId}><rect x={left} y={top} width={right - left} height={bottom - top} /></clipPath></defs>
          <g stroke={`var(--leaf-border, var(--border, ${colors.border}))`} strokeDasharray="3 3">
            {yTicks.map(value => <line key={value} x1={left} x2={right} y1={scaleY(value)} y2={scaleY(value)} />)}
            {xTicks.map((tick, index) => <line key={index} x1={tick.position} x2={tick.position} y1={top} y2={bottom} />)}
          </g>
          {xTicks.map((tick, index) => <text key={index} className="leaf-x-tick" x={tick.position} y={bottom + 18}
            textAnchor="middle" fill={ink} fontSize={12}>{formatX(tick.value)}</text>)}
          {yTicks.map(value => <text key={value} className="leaf-y-tick" x={left - 8} y={scaleY(value)}
            textAnchor="end" dominantBaseline="middle" fill={ink} fontSize={12}>{formatY(value)}</text>)}
          {xLabel && <text x={(left + right) / 2} y={height - 16} textAnchor="middle" fill={ink} fontSize={12}>{xLabel}</text>}
          <g clipPath={`url(#${clipId})`}>
            {referenceLine && <g>
              <line className="leaf-reference-line" x1={left} x2={right} y1={scaleY(referenceLine.y)} y2={scaleY(referenceLine.y)}
                stroke={referenceLine.color ?? `var(--leaf-chart-2, var(--chart-2, ${colors["chart-2"]}))`} strokeDasharray="3 3" />
              <text x={(left + right) / 2} y={scaleY(referenceLine.y) - 4} textAnchor="middle" fill={ink} fontSize={12}>{referenceLine.label}</text>
            </g>}
            {segments.filter(points => points.length > 1).map((points, index) => <path key={index}
              className="leaf-line-curve" d={monotonePath(points)} fill="none" stroke={color} strokeWidth={2} />)}
          </g>
          {points.map((point, index) => <circle key={index} className="leaf-line-dot" cx={point.x} cy={point.y}
            r={activeIndex === index ? 4 : 3} fill={color} />)}
        </svg>
        {active && <div role="tooltip" style={{ position: "absolute", pointerEvents: "none",
          left: tooltipLeft, top: tooltipTop, maxWidth: width - tooltipLeft, maxHeight: height - tooltipTop,
          overflow: "auto", overflowWrap: "anywhere", boxSizing: "border-box", padding: 10,
          backgroundColor: `var(--leaf-card, var(--card, ${colors.card}))`,
          color: `var(--leaf-foreground, var(--foreground, ${colors.foreground}))`,
          border: `1px solid var(--leaf-border, var(--border, ${colors.border}))`, borderRadius: "var(--leaf-radius, 0.5rem)" }}>
          {tooltipText}
        </div>}
        <div aria-live="polite" style={srOnly}>{tooltipText}</div>
      </div>
    </div>
  );
}
