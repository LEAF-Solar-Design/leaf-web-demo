import React from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DatasetElementV1, DatasetReadV1 } from "../../assistant-contract/src/integration";
import {
  DatasetElement, DataTableCore, LineChartCore, projectDatasetLine,
  formatDatasetAxisValue, resolveDisplayPreferences,
} from "../src/datasets";

const chart = vi.hoisted(() => ({
  data: [] as unknown[], line: {} as Record<string, unknown>, axis: {} as Record<string, unknown>,
  yAxis: {} as Record<string, unknown>, root: {} as Record<string, unknown>,
  tooltip: {} as Record<string, unknown>, reference: {} as Record<string, unknown>,
}));
// Replace only the chart layout boundary; projection and formatting remain real.
vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children, data, ...props }: { children: React.ReactNode; data: unknown[] }) => {
    chart.data = data; chart.root = props;
    return <div data-testid="native-line">{children}</div>;
  },
  Line: (props: Record<string, unknown>) => { chart.line = props; return null; },
  XAxis: (props: Record<string, unknown>) => { chart.axis = props; return null; },
  YAxis: (props: Record<string, unknown>) => { chart.yAxis = props; return null; },
  Tooltip: (props: Record<string, unknown>) => { chart.tooltip = props; return null; },
  ReferenceLine: (props: Record<string, unknown>) => { chart.reference = props; return null; },
  CartesianGrid: () => null,
}));

afterEach(cleanup);
const ref = { id: "sales", version: 1, digest: "a".repeat(64) };
const preferences = resolveDisplayPreferences();
const element: DatasetElementV1 = {
  schema_version: 1, id: "sales-table", title: "Sales", dataset_ref: ref,
  query: { fields: ["day", "revenue"], limit: 10 }, kind: "dataset.table",
  props: { columns: ["day", "revenue"], row_limit: 10 },
};
const lineElement = { ...element, kind: "dataset.line", props: { x_field: "day", y_field: "revenue" } };
function read(status: DatasetReadV1["status"] = "ready"): DatasetReadV1 {
  return {
    schema_version: 1, dataset_ref: ref, status,
    fields: [
      { id: "day", label: "Day", type: { kind: "date" }, nullable: false, sensitivity: "public" },
      { id: "revenue", label: "Revenue", type: { kind: "money", currency: "USD", scale: "minor" }, nullable: true, sensitivity: "public" },
    ],
    rows: ["empty", "forbidden", "invalid", "unavailable", "failed"].includes(status) ? [] : [
      { key: "one", values: { day: "2026-01-01", revenue: 0 } },
      { key: "two", values: { day: "2026-01-02", revenue: null } },
      { key: "three", values: { day: "2026-01-03", revenue: 1234 } },
    ],
    next_cursor: "secret-opaque-cursor",
    freshness: { source_updated_at: "2026-01-01T01:00:00Z", synced_at: "2026-01-01T02:00:00Z", fetched_at: "2026-01-01T03:00:00Z" },
  };
}

describe("native dataset elements", () => {
  it("renders semantic headers, formatting and separate freshness labels", () => {
    const result = render(<DatasetElement element={element} read={read()} preferences={preferences} />);
    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Revenue" }).getAttribute("scope")).toBe("col");
    for (const value of ["$0.00", "Unknown", "$12.34"]) expect(within(table).getByText(value)).toBeTruthy();
    expect(result.container.querySelectorAll("time")).toHaveLength(3);
    for (const label of ["Source updated", "Successfully synchronized", "Fetched"]) expect(screen.getByText(label)).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Sales" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "Sales data" }).tabIndex).toBe(0);
    expect(result.container.textContent).not.toContain("secret-opaque-cursor");
  });

  it("applies the smaller query and table row limits without mutating inputs", () => {
    const input = read();
    const before = JSON.stringify(input);
    const result = render(<DatasetElement element={{ ...element, props: { columns: ["day", "revenue"], row_limit: 1 } }}
      read={input} preferences={preferences} />);
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.queryByText("$12.34")).toBeNull();
    result.rerender(<DatasetElement element={{ ...element, query: { fields: ["day", "revenue"], limit: 2 } }}
      read={input} preferences={preferences} />);
    expect(screen.getAllByRole("row")).toHaveLength(3);
    expect(JSON.stringify(input)).toBe(before);
  });

  it.each(["id", "version", "digest"] as const)("hides rows for mismatched binding %s", (key) => {
    const input = read();
    input.dataset_ref = { ...ref, [key]: key === "version" ? 2 : key === "digest" ? "b".repeat(64) : "other" };
    render(<DatasetElement element={element} read={input} preferences={preferences} />);
    expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByText("$12.34")).toBeNull();
  });

  it("rejects missing metadata, missing columns, malformed values and unsupported currency", () => {
    const result = render(<DatasetElement element={element} read={{ ...read(), fields: read().fields.slice(0, 1) }} preferences={preferences} />);
    expect(screen.queryByRole("table")).toBeNull();
    result.rerender(<DatasetElement element={{ ...element, props: { columns: ["missing"], row_limit: 10 } }} read={read()} preferences={preferences} />);
    expect(screen.queryByRole("table")).toBeNull();
    result.rerender(<DatasetElement element={element} read={{ ...read(), rows: [{ key: "bad", values: { day: "2026-02-30", revenue: 1 } }] }} preferences={preferences} />);
    expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
    const currency = read();
    currency.fields[1].type = { kind: "money", currency: "ZZZ", scale: "minor" };
    result.rerender(<DatasetElement element={element} read={currency} preferences={preferences} />);
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("rejects absent bindings, unrequested columns, missing axes and invalid preferences", () => {
    const result = render(<DatasetElement element={{ ...element, dataset_ref: undefined }} read={read()} preferences={preferences} />);
    expect(screen.queryByRole("table")).toBeNull();
    result.rerender(<DatasetElement element={{ ...element, query: { fields: ["day"], limit: 10 } }} read={read()} preferences={preferences} />);
    expect(screen.queryByRole("table")).toBeNull();
    result.rerender(<DatasetElement element={{ ...lineElement, props: { x_field: "missing", y_field: "revenue" } }} read={read()} preferences={preferences} />);
    expect(screen.queryByTestId("native-line")).toBeNull();
    result.rerender(<DatasetElement element={element} read={read()} preferences={{ ...preferences, timezone: "invalid" }} />);
    expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it.each(["forbidden", "invalid", "unavailable", "failed"] as const)("shows safe %s state and hides retained rows", (status) => {
    const result = render(<DatasetElement element={element} read={read(status)} preferences={preferences} />);
    expect(screen.getByRole("status").textContent).not.toBe("");
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.getByText("Fetched")).toBeTruthy();
    result.rerender(<DatasetElement element={element} read={{ ...read(), status }} preferences={preferences} />);
    expect(screen.queryByText("$12.34")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it.each(["partial", "stale"] as const)("retains validated %s data with status and guarded callbacks", (status) => {
    const onRetry = vi.fn();
    const onLoadMore = vi.fn();
    const props = { element, read: read(status), preferences, onRetry, onLoadMore };
    const result = render(<DatasetElement {...props} />);
    expect(screen.getByRole("status").textContent?.toLowerCase()).toContain(status);
    expect(screen.getByText("$12.34")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onLoadMore).toHaveBeenCalledWith("secret-opaque-cursor");
    result.rerender(<DatasetElement {...props} isLoading />);
    for (const name of ["Retry", "Load more"]) {
      const button = screen.getByRole("button", { name }) as HTMLButtonElement;
      expect(button.disabled).toBe(true);
      fireEvent.click(button);
    }
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    expect(result.container.textContent).not.toContain("secret-opaque-cursor");
  });

  it("shows loading and empty states without inventing rows or callbacks", () => {
    const retry = vi.fn();
    const result = render(<DatasetElement element={element} read={null} preferences={preferences} isLoading onRetry={retry} />);
    expect(screen.getByText("Loading dataset…")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
    result.rerender(<DatasetElement element={element} read={read("empty")} preferences={preferences} />);
    expect(screen.getByText("No data available.")).toBeTruthy();
    expect(screen.queryByText("$0.00")).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
    expect(retry).not.toHaveBeenCalled();
  });

  it("uses null gaps and a native keyboard-operable equivalent table", () => {
    const original = read();
    const snapshot = JSON.stringify(original);
    const result = render(<DatasetElement element={lineElement} read={original} preferences={preferences} />);
    expect(chart.data).toEqual([
      { x: Date.parse("2026-01-01"), y: 0 }, { x: Date.parse("2026-01-02"), y: null }, { x: Date.parse("2026-01-03"), y: 1234 },
    ]);
    expect(chart.line.connectNulls).toBe(false);
    expect(chart.line.isAnimationActive).toBe(false);
    expect(chart.root.accessibilityLayer).toBe(true);
    const summary = screen.getByText("View data table");
    expect(summary.tagName).toBe("SUMMARY");
    expect(summary.parentElement?.tagName).toBe("DETAILS");
    summary.parentElement?.setAttribute("open", "");
    const table = screen.getByRole("table");
    expect(within(table).getByText("Unknown")).toBeTruthy();
    expect(within(table).getByText("$12.34")).toBeTruthy();
    expect(JSON.stringify(original)).toBe(snapshot);
    result.rerender(<DatasetElement element={{ ...lineElement, props: { x_field: "revenue", y_field: "day" } }} read={original} preferences={preferences} />);
    expect(screen.getByText("Invalid dataset or binding.")).toBeTruthy();
    expect(screen.queryByTestId("native-line")).toBeNull();
  });

  it("escapes provider text without creating controls or displaying raw errors", () => {
    const raw = read();
    raw.fields[0].type = { kind: "text" };
    raw.rows[0].values.day = "<button>Steal</button>";
    const result = render(<DatasetElement element={element} read={raw} preferences={preferences} />);
    expect(screen.getByText("<button>Steal</button>")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Steal" })).toBeNull();
    result.rerender(<DatasetElement element={element} read={{ error: "private provider secret" }} preferences={preferences} />);
    expect(result.container.textContent).not.toContain("private provider secret");
  });

  it.each(["date", "instant", "number", "count"] as const)(
    "preserves real %s x spacing, sorts only the plot and omits missing positions",
    (kind) => {
      const input = read();
      input.fields[0] = { ...input.fields[0], type: { kind }, nullable: true };
      const xs = kind === "date" ? ["2026-02-01", "2026-01-01", "2026-01-02"] :
        kind === "instant" ? ["2026-01-01T01:00:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"] : [100, 1, 2];
      input.rows = xs.map((x, i) => ({ key: String(i), values: { day: x, revenue: i === 2 ? null : i } }));
      input.rows.push({ key: "missing", values: { day: null, revenue: 3 } });
      const before = JSON.stringify(input);
      const projected = projectDatasetLine(input.rows, input.fields[0], input.fields[1]);
      render(<DatasetElement element={lineElement} read={input} preferences={preferences} />);
      const positions = xs.map((x) => typeof x === "number" ? x : Date.parse(x)).sort((a, b) => a - b);
      expect(chart.data).toEqual([
        { x: positions[0], y: 1 }, { x: positions[1], y: null }, { x: positions[2], y: 0 },
      ]);
      expect(projected).toEqual(chart.data);
      expect(positions[2] - positions[1]).toBeGreaterThan(positions[1] - positions[0]);
      expect(chart.axis.type).toBe("number");
      expect(chart.axis.domain).toEqual(["dataMin", "dataMax"]);
      expect(chart.axis.allowDecimals).toBe(kind !== "count");
      expect((chart.axis.tickFormatter as (v: number) => string)(positions[0])).not.toBe("Invalid");
      screen.getByText("View data table").parentElement?.setAttribute("open", "");
      const tableRows = within(screen.getByRole("table")).getAllByRole("row");
      expect(tableRows).toHaveLength(5);
      expect(within(tableRows[1]).getAllByRole("cell")[0].textContent)
        .toBe(formatDatasetAxisValue(input.fields[0], xs[0], preferences));
      expect(within(tableRows[4]).getAllByRole("cell")[0].textContent).toBe("Unknown");
      expect(JSON.stringify(input)).toBe(before);
    },
  );

  it.each(["1969-12-31T23:59:59", "1970-01-01T00:00:00", "2026-01-01T00:00:00"])(
    "preserves submillisecond instant spacing at Number precision near %s",
    (second) => {
      const input = read();
      input.fields[0] = { ...input.fields[0], type: { kind: "instant" }, nullable: true };
      const early = `${second}.0001Z`;
      const late = `${second}.0009Z`;
      input.rows = [
        { key: "late", values: { day: late, revenue: 9 } },
        { key: "early", values: { day: early, revenue: 1 } },
        { key: "tie", values: { day: early, revenue: null } },
        { key: "missing", values: { day: null, revenue: 3 } },
      ];
      const before = JSON.stringify(input);
      const epoch = Date.parse(`${second}Z`);
      const projected = projectDatasetLine(input.rows, input.fields[0], input.fields[1]);
      expect(projected).toEqual([
        { x: epoch + 0.1, y: 1 }, { x: epoch + 0.1, y: null }, { x: epoch + 0.9, y: 9 },
      ]);
      expect(projected[2].x).toBeGreaterThan(projected[0].x);
      const tolerance = Number.EPSILON * Math.max(1, Math.abs(epoch));
      expect(Math.abs(projected[2].x - projected[0].x - 0.8)).toBeLessThanOrEqual(tolerance);
      render(<DatasetElement element={lineElement} read={input} preferences={preferences} />);
      expect(chart.data).toEqual(projected);
      expect(chart.line.connectNulls).toBe(false);
      screen.getByText("View data table").parentElement?.setAttribute("open", "");
      expect(within(screen.getByRole("table")).getAllByRole("row")).toHaveLength(5);
      expect(JSON.stringify(input)).toBe(before);
    },
  );

  it.each(["number", "count", "ratio", "money", "duration"] as const)("supports a numeric X and %s Y", (kind) => {
    const input = read();
    input.fields[0].type = { kind: "number" };
    input.fields[1].type = kind === "ratio" ? { kind, scale: "fraction" } :
      kind === "money" ? { kind, currency: "USD", scale: "minor" } :
      kind === "duration" ? { kind, unit: "s" } : { kind };
    input.rows = [{ key: "zero", values: { day: 0, revenue: 0 } }];
    render(<DatasetElement element={lineElement} read={input} preferences={preferences} />);
    expect(screen.getByTestId("native-line")).toBeTruthy();
    expect(chart.data).toEqual([{ x: 0, y: 0 }]);
    expect(chart.yAxis.allowDecimals).toBe(kind !== "count" && kind !== "money");
  });

  it.each(["date", "instant"] as const)("guards generated %s ticks outside the canonical range", (kind) => {
    const field = { ...read().fields[0], type: { kind } };
    for (const value of [Infinity, NaN, 1e20, -1e20, Date.parse("+010000-01-01T00:00:00Z")]) {
      expect(formatDatasetAxisValue(field, value, preferences)).toBe("Invalid");
    }
    expect(formatDatasetAxisValue(field, Date.parse("2026-01-01T00:00:00Z"), preferences)).toContain("Jan 1, 2026");
  });

  it("retains categorical defaults, chart options, formatter callbacks and accessible description", () => {
    render(<LineChartCore data={[{ day: "A", value: 2 }]} xKey="day" yKey="value" title="Standalone"
      referenceLine={{ y: 3, label: "Target" }} yAxisDomain={[0, 5]}
      xFormatter={(value) => `X ${value}`} yFormatter={(value) => `Y ${value}`}
      accessibilityDescription="Values are also in the report." />);
    expect(chart.axis.type).toBe("category");
    expect(chart.yAxis.domain).toEqual([0, 5]);
    expect(chart.reference.y).toBe(3);
    expect(chart.line.connectNulls).toBe(false);
    expect((chart.tooltip.labelFormatter as (value: unknown) => string)("A")).toBe("X A");
    expect((chart.tooltip.formatter as (value: unknown) => unknown)(2)).toEqual(["Y 2", "value"]);
    const group = screen.getByRole("group", { name: "Standalone" });
    expect(group.getAttribute("aria-describedby")).toBe(screen.getByText("Values are also in the report.").id);
  });

  it("bounds table viewport without losing the equivalent rows and supplies a meaningful empty state", () => {
    const columns = [{ key: "value", header: "Value", cell: (row: { key: string }) => row.key }];
    const rows = Array.from({ length: 20 }, (_, i) => ({ key: String(i) }));
    const result = render(<DataTableCore columns={columns} rows={rows} getRowKey={(row) => row.key} maxRows={2} />);
    const region = screen.getByRole("region", { name: "Dataset data" });
    expect(region.tabIndex).toBe(0);
    expect(region.style.overflow).toBe("auto");
    expect(region.style.maxHeight).toBe("116px");
    expect(screen.getAllByRole("row")).toHaveLength(21);
    result.rerender(<DataTableCore columns={columns} rows={[]} getRowKey={(row) => row.key} />);
    expect(screen.getByText("There are no rows to display.")).toBeTruthy();
  });
});
