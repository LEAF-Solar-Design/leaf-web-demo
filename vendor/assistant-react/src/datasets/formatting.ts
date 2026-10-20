import { z } from "zod";
import { DatasetFieldV1 } from "../../../assistant-contract/src/integration";

export const DisplayPreferencesSchema = z.strictObject({
  locale: z.string().min(1).refine((value) => {
    try {
      return Intl.DateTimeFormat.supportedLocalesOf([value]).length === 1;
    } catch { return false; }
  }, "Unsupported locale"),
  timezone: z.string().min(1).refine((value) => {
    try {
      if (/^[+-]/.test(value)) return false;
      new Intl.DateTimeFormat("en-US", { timeZone: value });
      return true;
    } catch { return false; }
  }, "Invalid timezone"),
  hourFormat: z.enum(["12h", "24h"]),
});
export type DisplayPreferences = z.infer<typeof DisplayPreferencesSchema>;

export class DisplayPreferencesError extends Error {
  constructor() {
    super("Invalid display preferences");
    this.name = "DisplayPreferencesError";
  }
}

export function resolveDisplayPreferences(
  user?: Partial<DisplayPreferences>,
  workspace?: Partial<DisplayPreferences>,
): DisplayPreferences {
  const schema = DisplayPreferencesSchema.partial();
  const u = schema.safeParse(user ?? {});
  const w = schema.safeParse(workspace ?? {});
  if (!u.success || !w.success) throw new DisplayPreferencesError();
  return {
    locale: u.data.locale ?? w.data.locale ?? "en-US",
    timezone: u.data.timezone ?? w.data.timezone ?? "UTC",
    hourFormat: u.data.hourFormat ?? w.data.hourFormat ?? "24h",
  };
}

function validDate(value: string): boolean {
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/** Format a canonical scalar without changing its reporting bucket or currency. */
export function formatDatasetValue(
  field: DatasetFieldV1,
  value: string | number | boolean | null,
  preferences: DisplayPreferences,
): string {
  const prefs = resolveDisplayPreferences(preferences);
  if (!DatasetFieldV1.safeParse(field).success) return "Invalid";
  if (value === null) return field.nullable ? "Unknown" : "Invalid";
  const type = field.type;
  if (type.kind === "text" || type.kind === "identifier") {
    return typeof value === "string" && (type.kind === "text" || value.length > 0) ? value : "Invalid";
  }
  if (type.kind === "boolean") return typeof value === "boolean" ? (value ? "True" : "False") : "Invalid";
  if (type.kind === "date" || type.kind === "instant") {
    if (typeof value !== "string") return "Invalid";
    const pattern = type.kind === "date" ? /^\d{4}-\d{2}-\d{2}(?![\s\S])/ :
      /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?Z(?![\s\S])/;
    if (!pattern.test(value) || !validDate(value.slice(0, 10))) return "Invalid";
    return new Intl.DateTimeFormat(prefs.locale, {
      year: "numeric", month: "short", day: "numeric",
      timeZone: type.kind === "date" ? "UTC" : prefs.timezone,
      ...(type.kind === "instant" ? {
        hour: "2-digit", minute: "2-digit", second: "2-digit",
        hourCycle: prefs.hourFormat === "12h" ? "h12" as const : "h23" as const,
        timeZoneName: "short" as const,
      } : {}),
    }).format(new Date(type.kind === "date" ? `${value}T00:00:00Z` : value));
  }
  if (typeof value !== "number" || !Number.isFinite(value)) return "Invalid";
  if (type.kind === "count" && (!Number.isSafeInteger(value) || value < 0)) return "Invalid";
  if (type.kind === "money") {
    if (type.scale === "minor" && !Number.isSafeInteger(value)) return "Invalid";
    // Intl otherwise accepts arbitrary three-letter codes as a two-digit currency.
    if (!Intl.supportedValuesOf("currency").includes(type.currency)) return "Invalid";
    const formatter = new Intl.NumberFormat(prefs.locale, { style: "currency", currency: type.currency });
    const digits = formatter.resolvedOptions().maximumFractionDigits ?? 2;
    return formatter.format(type.scale === "minor" ? value / 10 ** digits : value);
  }
  if (type.kind === "ratio") {
    return new Intl.NumberFormat(prefs.locale, { style: "percent", maximumFractionDigits: 12 })
      .format(type.scale === "fraction" ? value : value / 100);
  }
  const precision = type.kind === "number" ? type.precision : undefined;
  const formatted = new Intl.NumberFormat(prefs.locale, {
    minimumFractionDigits: precision, maximumFractionDigits: precision ?? 12,
  }).format(value);
  return (type.kind === "number" && type.unit) || type.kind === "duration"
    ? `${formatted} ${type.unit}` : formatted;
}
