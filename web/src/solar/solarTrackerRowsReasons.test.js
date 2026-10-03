// @vitest-environment node
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { TRACKER_ROWS_REASONS } from "./solarTrackerRowsReasons.js";

const EXPECTED = {
  TRACKER_ROWS_OPERATION_UNSUPPORTED: "Choose the manual tracker row creation operation.",
  TRACKER_ROWS_REQUEST_INVALID: "Send a valid tracker row creation request.",
  TRACKER_ROWS_ROWS_INVALID: "Enter at least one tracker row.",
  TRACKER_ROWS_ROW_INVALID: "Enter two finite axis coordinates, a width and an integer slot count for each row.",
  TRACKER_ROWS_POWER_INVALID: "Enter a positive module power within the supported numeric limit.",
  TRACKER_ROWS_HEAD_INVALID: "Refresh the physical state before publishing tracker rows.",
  TRACKER_ROWS_UNITS_UNSUPPORTED: "Use a drawing measured in metres or feet.",
  TRACKER_ROWS_UNITS_MISMATCH: "The drawing and physical state must use the same units.",
  TRACKER_ROWS_FRAME_UNSUPPORTED: "Use an untransformed world coordinate frame.",
  TRACKER_ROWS_SLOTS_INVALID: "Enter an integer slot count from 1 to 10,000 for each row.",
  TRACKER_ROWS_AXIS_INVALID: "Enter a nonzero row axis within the supported geometry limits.",
  TRACKER_ROWS_WIDTH_INVALID: "Enter a positive cross-axis width within the supported geometry limits.",
  TRACKER_ROWS_LIMIT_EXCEEDED: "Use at most 256 rows, 100,000 total slots and 256 KiB of request data.",
  TRACKER_ROWS_PROJECT_ID_INVALID: "Choose a valid Solar project.",
  TRACKER_ROWS_PROJECT_MISMATCH: "Open the drawing in its recorded Solar project.",
  TRACKER_ROWS_DRAWING_NOT_FOUND: "Open an available drawing before publishing tracker rows.",
  TRACKER_ROWS_GRAPH_REQUIRED: "Create a persisted Solar design graph before publishing tracker rows.",
  TRACKER_ROWS_GROUND_REQUIRED: "Set the installation design to Ground before publishing tracker rows.",
  TRACKER_ROWS_GRAPH_CONVERTED: "This drawing already records tracker conversion, so tracker rows cannot be created here.",
  TRACKER_ROWS_DEPENDENT_STATE: "Existing physical outputs prevent tracker row creation because their independence cannot be proved.",
  TRACKER_ROWS_ALREADY_EXISTS: "This drawing already carries tracker rows, and this operation only creates them once.",
  TRACKER_ROWS_STALE_HEAD: "The physical state changed, so refresh it before publishing tracker rows.",
  TRACKER_ROWS_STATE_INVALID: "The stored physical state cannot support tracker row creation.",
  TRACKER_ROWS_WRITES_DRAINED: "Drawing changes are temporarily paused.",
  TRACKER_ROWS_STORE_UNAVAILABLE: "The drawing store is temporarily unavailable.",
  TRACKER_ROWS_STORE_UNSAFE: "The drawing store cannot safely publish tracker rows.",
  TRACKER_ROWS_LOG_FULL: "The physical state history has reached its publication limit.",
  TRACKER_ROWS_CHECKOUT_REQUIRED: "Use the active drawing checkout to publish tracker rows.",
  TRACKER_ROWS_CHECKOUT_UNAVAILABLE: "The drawing checkout service is temporarily unavailable.",
  TRACKER_ROWS_CONTENT_TYPE_UNSUPPORTED: "Send the tracker row request as JSON.",
};

describe("manual tracker row refusal vocabulary", () => {
  it("matches the server module's complete code set", () => {
    const server = readFileSync(new URL("../../../server/solar_tracker_rows.py", import.meta.url), "utf8");
    const codes = [...new Set(server.match(/\bTRACKER_ROWS_[A-Z_]+\b/g))].sort();
    expect(codes).toEqual(Object.keys(TRACKER_ROWS_REASONS).sort());
    expect(codes).toHaveLength(30);
  });

  it("pins all thirty literal sentences", () => {
    expect(TRACKER_ROWS_REASONS).toEqual(EXPECTED);
  });

  it("freezes the exported map", () => {
    expect(Object.isFrozen(TRACKER_ROWS_REASONS)).toBe(true);
  });

  it("uses literal sentences without typographic or encoded dashes", () => {
    const source = readFileSync(new URL("./solarTrackerRowsReasons.js", import.meta.url), "utf8");
    for (const [code, value] of Object.entries(TRACKER_ROWS_REASONS)) {
      expect(typeof value).toBe("string");
      expect(value.length).toBeGreaterThanOrEqual(12);
      expect(value).toMatch(/^[A-Z].*\.$/);
      expect(value).not.toMatch(/[\u2013\u2014]|\\u201[34]|&(?:mdash|ndash|#821[12]|#x201[34]);/i);
      expect(source).toContain(`${code}: ${JSON.stringify(value)}`);
    }
  });
});
