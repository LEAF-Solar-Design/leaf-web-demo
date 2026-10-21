import { describe, expect, it } from "vitest";
import type { DatasetFieldV1 } from "../../assistant-contract/src/integration";
import {
  DisplayPreferencesSchema, DisplayPreferencesError, formatDatasetValue, resolveDisplayPreferences,
} from "../src/datasets/formatting";

const prefs = resolveDisplayPreferences();
const field = (type: DatasetFieldV1["type"], nullable = true): DatasetFieldV1 =>
  ({ id: "value", label: "Value", type, nullable, sensitivity: "public" });
const format = (type: DatasetFieldV1["type"], value: string | number | boolean | null) =>
  formatDatasetValue(field(type), value, prefs);

describe("dataset formatting", () => {
  it("resolves each preference independently and rejects invalid saved settings", () => {
    expect(prefs).toEqual({ locale: "en-US", timezone: "UTC", hourFormat: "24h" });
    expect(resolveDisplayPreferences({ locale: "de-DE" }, { timezone: "Asia/Tokyo", hourFormat: "12h" }))
      .toEqual({ locale: "de-DE", timezone: "Asia/Tokyo", hourFormat: "12h" });
    expect(resolveDisplayPreferences({ timezone: "UTC", hourFormat: "24h" },
      { locale: "fr-FR", timezone: "Asia/Tokyo", hourFormat: "12h" }))
      .toEqual({ locale: "fr-FR", timezone: "UTC", hourFormat: "24h" });
    expect(() => resolveDisplayPreferences({ timezone: "Mars/Olympus" })).toThrow(DisplayPreferencesError);
    expect(() => resolveDisplayPreferences({ locale: "not_a_locale" })).toThrow(DisplayPreferencesError);
    expect(() => resolveDisplayPreferences({}, { timezone: "+01:00" })).toThrow(DisplayPreferencesError);
    expect(DisplayPreferencesSchema.safeParse({ ...prefs, hourFormat: "auto" }).success).toBe(false);
    expect(formatDatasetValue(field({ kind: "number", precision: 2 }), 1234.5,
      resolveDisplayPreferences({ locale: "de-DE" }))).toBe("1.234,50");
  });

  it("distinguishes unknown, zero, identifiers and invalid values", () => {
    expect(format({ kind: "count" }, null)).toBe("Unknown");
    expect(format({ kind: "count" }, 0)).toBe("0");
    expect(format({ kind: "identifier" }, "2026-01-01")).toBe("2026-01-01");
    expect(format({ kind: "text" }, "<button>Retry</button>")).toBe("<button>Retry</button>");
    expect(format({ kind: "boolean" }, true)).toBe("True");
    expect(format({ kind: "boolean" }, false)).toBe("False");
    for (const value of ["12", NaN, Infinity, true]) expect(format({ kind: "number" }, value)).toBe("Invalid");
    for (const value of [-1, 1.5, Number.MAX_SAFE_INTEGER + 1]) expect(format({ kind: "count" }, value)).toBe("Invalid");
    expect(format({ kind: "date" }, "2026-02-30")).toBe("Invalid");
    expect(format({ kind: "date" }, "2026-01-01\n")).toBe("Invalid");
    expect(format({ kind: "instant" }, "2026-01-01T00:00:00+01:00")).toBe("Invalid");
    expect(format({ kind: "instant" }, "2026-01-01T25:00:00Z")).toBe("Invalid");
    expect(formatDatasetValue(field({ kind: "number" }, false), null, prefs)).toBe("Invalid");
  });

  it("keeps dates fixed across viewer timezones and converts instants across midnight and DST", () => {
    const west = resolveDisplayPreferences({ timezone: "America/Los_Angeles" });
    const east = resolveDisplayPreferences({ timezone: "Asia/Tokyo", hourFormat: "12h" });
    const date = field({ kind: "date" });
    expect(formatDatasetValue(date, "2026-01-01", west)).toBe("Jan 1, 2026");
    expect(formatDatasetValue(date, "2026-01-01", east)).toBe("Jan 1, 2026");
    const instant = field({ kind: "instant" });
    expect(formatDatasetValue(instant, "2026-01-01T01:00:00Z", west)).toContain("Dec 31, 2025");
    expect(formatDatasetValue(instant, "2026-01-01T01:00:00Z", east)).toContain("10:00:00 AM");
    expect(formatDatasetValue(instant, "2026-03-08T09:59:00Z", west)).toContain("01:59:00 PST");
    expect(formatDatasetValue(instant, "2026-03-08T10:00:00Z", west)).toContain("03:00:00 PDT");
  });

  it("uses currency fraction digits without FX conversion", () => {
    expect(format({ kind: "money", currency: "USD", scale: "minor" }, 1234)).toBe("$12.34");
    expect(format({ kind: "money", currency: "JPY", scale: "minor" }, 1234)).toBe("¥1,234");
    expect(format({ kind: "money", currency: "KWD", scale: "minor" }, 1234)).toMatch(/KWD\s*1\.234/);
    expect(format({ kind: "money", currency: "USD", scale: "major" }, 1234)).toBe("$1,234.00");
    expect(format({ kind: "money", currency: "USD", scale: "minor" }, 1.2)).toBe("Invalid");
    expect(format({ kind: "money", currency: "ZZZ", scale: "minor" }, 12)).toBe("Invalid");
  });

  it("honors explicit ratio scale, precision and duration units without clamping", () => {
    expect(format({ kind: "ratio", scale: "fraction" }, 0.125)).toBe("12.5%");
    expect(format({ kind: "ratio", scale: "percent" }, 12.5)).toBe("12.5%");
    expect(format({ kind: "ratio", scale: "fraction" }, -0.25)).toBe("-25%");
    expect(format({ kind: "ratio", scale: "fraction" }, 1.5)).toBe("150%");
    expect(format({ kind: "ratio", scale: "percent" }, -25)).toBe("-25%");
    expect(format({ kind: "ratio", scale: "percent" }, 150)).toBe("150%");
    expect(format({ kind: "number", precision: 2, unit: "kWh" }, 1.234)).toBe("1.23 kWh");
    expect(format({ kind: "duration", unit: "minutes" }, 90)).toBe("90 minutes");
  });
});
