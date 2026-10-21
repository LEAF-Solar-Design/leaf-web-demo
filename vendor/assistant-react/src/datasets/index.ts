"use client";

// Deliberately separate from the conversation barrel: only dataset consumers load Recharts.
export { DatasetElement } from "./DatasetElement";
export type { DatasetElementProps } from "./DatasetElement";
export { DataTableCore } from "./DataTableCore";
export type { DataTableColumn, DataTableCoreProps } from "./DataTableCore";
export { LineChartCore, projectDatasetLine, formatDatasetAxisValue } from "./LineChartCore";
export type { LineChartCoreProps, LineChartProps, DatasetChartPoint } from "./LineChartCore";
export {
  DisplayPreferencesSchema, DisplayPreferencesError, resolveDisplayPreferences, formatDatasetValue,
} from "./formatting";
export type { DisplayPreferences } from "./formatting";
