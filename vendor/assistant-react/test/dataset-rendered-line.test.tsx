import React from "react";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import { DatasetElement, resolveDisplayPreferences } from "../src/datasets";
import type { DatasetElementV1, DatasetReadV1 } from "../../assistant-contract/src/integration";

// Supply layout dimensions only. Actual Recharts renders the SVG marks and ticks.
vi.mock("recharts", async (original) => {
  const actual = await original<typeof import("recharts")>();
  return { ...actual, ResponsiveContainer: ({ children }: { children: React.ReactElement }) =>
    React.cloneElement(children, { width: 700, height: 300 } as object) };
});
afterEach(cleanup);

it("renders isolated observations around nulls and unique calendar ticks", async () => {
  const ref = { id: "observations", version: 1, digest: "a".repeat(64) };
  const element: DatasetElementV1 = {
    schema_version: 1, id: "line", title: "Revenue", kind: "dataset.line",
    dataset_ref: ref, query: { fields: ["day", "amount"], limit: 10 },
    props: { x_field: "day", y_field: "amount" },
  };
  const read: DatasetReadV1 = {
    schema_version: 1, dataset_ref: ref, status: "ready", next_cursor: null,
    fields: [
      { id: "day", label: "Date", type: { kind: "date" }, nullable: false, sensitivity: "public" },
      { id: "amount", label: "Revenue", type: { kind: "money", currency: "USD", scale: "minor" }, nullable: true, sensitivity: "public" },
    ],
    rows: [1200, null, 3400].map((amount, index) => ({
      key: `row-${index}`, values: { day: `2026-10-${20 + index}`, amount },
    })),
    freshness: { source_updated_at: null, synced_at: null, fetched_at: "2026-10-20T12:00:00Z" },
  };
  const { container } = render(<DatasetElement element={element} read={read} preferences={resolveDisplayPreferences()} />);
  await waitFor(() => expect(container.querySelectorAll("circle.recharts-line-dot")).toHaveLength(2));
  const marks = [...container.querySelectorAll("circle.recharts-line-dot")];
  const positions = marks.map(mark => [Number(mark.getAttribute("cx")), Number(mark.getAttribute("cy"))]);
  expect(positions.flat().every(Number.isFinite)).toBe(true);
  expect(positions[0][0]).toBeLessThan(positions[1][0]);
  expect(positions[0][1]).toBeGreaterThan(positions[1][1]);
  const curve = container.querySelector(".recharts-line-curve")?.getAttribute("d") ?? "";
  // A gap between two isolated observations must not produce a joining segment.
  expect(curve).not.toMatch(/[LC]/i);
  const ticks = [...container.querySelectorAll(".recharts-cartesian-axis-tick-value")]
    .map(tick => tick.textContent).filter(value => value?.includes("2026"));
  expect(ticks).toEqual(["Oct 20, 2026", "Oct 21, 2026", "Oct 22, 2026"]);
  expect(read.rows[1].values.amount).toBeNull();
});
