import { useState } from "react";
import type { BlockV1 } from "../../../assistant-contract/src/index";
import { formatViewValue, formatWhen } from "../states";
import { SERIES, color, eyebrow, figure, meta, secondaryButton } from "../tokens";

export type ViewBlock = Extract<BlockV1, { type: "view" }>;
type ViewRow = ViewBlock["rows"][number];

/** A table shows this many rows until the user asks for the rest (the contract caps a view at 50). */
export const TABLE_VISIBLE_ROWS = 10;

export interface AssistantViewPanelProps {
  view: ViewBlock;
  /** invocation_id to the server's tool label, for the provenance line. */
  toolLabels?: ReadonlyMap<string, string>;
  now: number;
}

/** "From Read today's books · as of 11:00 AM CT": which server reads the view came from, and when. */
export function provenanceLine(view: ViewBlock, toolLabels: ReadonlyMap<string, string> | undefined, now: number): string {
  const labels = new Set<string>();
  for (const id of view.invocation_ids) {
    const label = toolLabels?.get(id);
    if (label) labels.add(label);
  }
  const count = view.invocation_ids.length;
  const source = labels.size > 0 ? [...labels].join(", ") : `${count} server read${count === 1 ? "" : "s"}`;
  return `From ${source} · as of ${formatWhen(view.as_of, now)}`;
}

function Figure({ row, size }: { row: ViewRow; size: number }) {
  const value = formatViewValue(row.value, row.unit);
  return (
    <span data-figure="mono" aria-label={value.label} style={{ ...figure, fontSize: size, fontWeight: 600 }}>
      {value.text}
    </span>
  );
}

function MetricRows({ rows }: { rows: readonly ViewRow[] }) {
  return (
    <dl style={{ margin: 0, display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(140px, 1fr))", gap: 12 }}>
      {rows.map((row, index) => (
        <div key={`${row.invocation_id}-${index}`}>
          <dt style={meta}>{row.label}</dt>
          <dd style={{ margin: 0 }}>
            <Figure row={row} size={28} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

function BoundedTable({ rows }: { rows: readonly ViewRow[] }) {
  const [expanded, setExpanded] = useState(false);
  const shown = expanded ? rows : rows.slice(0, TABLE_VISIBLE_ROWS);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ maxHeight: 360, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
          <thead>
            <tr>
              <th scope="col" style={{ ...meta, textAlign: "left", fontWeight: 600, padding: "4px 0" }}>
                Item
              </th>
              <th scope="col" style={{ ...meta, textAlign: "right", fontWeight: 600, padding: "4px 0" }}>
                Value
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row, index) => (
              <tr key={`${row.invocation_id}-${index}`} style={{ borderTop: `1px solid ${color.border}` }}>
                <td style={{ padding: "6px 0" }}>{row.label}</td>
                <td style={{ padding: "6px 0", textAlign: "right" }}>
                  <Figure row={row} size={14} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > TABLE_VISIBLE_ROWS ? (
        <button type="button" style={{ ...secondaryButton, alignSelf: "flex-start" }} onClick={() => setExpanded((v) => !v)}>
          {expanded ? "Show fewer rows" : `Show all ${rows.length} rows`}
        </button>
      ) : null}
    </div>
  );
}

const LINE_WIDTH = 320;
const LINE_HEIGHT = 96;
const LINE_PAD = 4;

function LineView({ title, rows }: { title: string; rows: readonly ViewRow[] }) {
  const points: { value: number; unit: ViewRow["unit"] }[] = [];
  for (const row of rows) {
    if (typeof row.value === "number" && Number.isFinite(row.value)) points.push({ value: row.value, unit: row.unit });
  }
  if (points.length < 2) return <MetricRows rows={rows} />;
  let min = points[0].value;
  let max = points[0].value;
  for (const point of points) {
    if (point.value < min) min = point.value;
    if (point.value > max) max = point.value;
  }
  const span = max - min || 1;
  const step = (LINE_WIDTH - 2 * LINE_PAD) / (points.length - 1);
  const coords = points
    .map((point, index) => {
      const x = LINE_PAD + index * step;
      const y = LINE_HEIGHT - LINE_PAD - ((point.value - min) * (LINE_HEIGHT - 2 * LINE_PAD)) / span;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const unit = points[0].unit;
  const show = (value: number) => formatViewValue(value, unit).text;
  const summary = `${title}: ${points.length} points, from ${show(points[0].value)} to ${show(
    points[points.length - 1].value,
  )}, low ${show(min)}, high ${show(max)}.`;
  return (
    <figure style={{ margin: 0 }}>
      <svg
        role="img"
        aria-label={summary}
        viewBox={`0 0 ${LINE_WIDTH} ${LINE_HEIGHT}`}
        width="100%"
        height={LINE_HEIGHT}
        preserveAspectRatio="none"
      >
        <polyline
          data-series="chart-1"
          points={coords}
          fill="none"
          vectorEffect="non-scaling-stroke"
          style={{ stroke: SERIES[0], strokeWidth: 2 }}
        />
      </svg>
      <figcaption data-testid="assistant-line-summary" style={meta}>
        {summary}
      </figcaption>
    </figure>
  );
}

/**
 * A server view in the fixed ANSWER frame: not a card, no StateInk, so an
 * answer can never pose as consent. metric figures are Geist Mono, tables are
 * bounded, lines use the series palette and carry an accessibility summary.
 */
export function AssistantViewPanel({ view, toolLabels, now }: AssistantViewPanelProps) {
  return (
    <section
      data-testid="assistant-answer"
      data-view-kind={view.kind}
      aria-label={`Answer: ${view.title}`}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        paddingLeft: 12,
        borderLeft: `2px solid ${color.border}`,
      }}
    >
      <div style={eyebrow}>ANSWER</div>
      <div style={{ fontWeight: 600 }}>{view.title}</div>
      {view.kind === "metric" ? <MetricRows rows={view.rows} /> : null}
      {view.kind === "table" ? <BoundedTable rows={view.rows} /> : null}
      {view.kind === "line" ? <LineView title={view.title} rows={view.rows} /> : null}
      <div data-testid="assistant-provenance" style={meta}>
        {provenanceLine(view, toolLabels, now)}
      </div>
    </section>
  );
}
