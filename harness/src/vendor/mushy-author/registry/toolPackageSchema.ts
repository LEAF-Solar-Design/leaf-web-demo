/**
 * Faithful TypeScript port of the CONTRACT.md section 2 tool-package schema
 * (the "frozen tool-package schema" oracle).
 *
 * WHY a TS port and not a reuse of engine/selfcheck.py: the acceptance gate must
 * be hermetic and Windows-safe. Shelling out to python (selfcheck.py) from a Node
 * test adds a cross-language runtime dependency and Windows path/CRLF fragility,
 * and selfcheck.py is actually a section-3 ENVELOPE + effective-registry checker,
 * not a section-2 tool-PACKAGE schema checker. A pure-TS port keeps the oracle in
 * one language, one process, no subprocess. (selfcheck.py remains the section-3
 * envelope oracle on the Python/run side.)
 *
 * The rules below are transcribed field-by-field from CONTRACT.md section 2.
 */

import type { ToolPackage } from "../ports/index.js";

const KEBAB = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const SEMVER = /^\d+\.\d+\.\d+([-+][0-9A-Za-z-.]+)*$/;
// Pragmatic ISO-8601 (date, or date-time with optional fractional seconds + tz).
const ISO8601 = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$/;

const VALID_KINDS = new Set(["script", "appbundle"]);
const VALID_CAPS = new Set(["drawing.read", "drawing.write"]);
const VALID_AUTHORS = new Set(["agent", "user"]);

/**
 * Validate an unknown value against the frozen CONTRACT section 2 tool package.
 * Returns a list of human-readable diagnostics; empty === valid. Extra fields
 * (e.g. the SPEC section 7 `entry`/`timeout_ms`/`idempotent`/`review`) are
 * permitted — section 2 freezes a minimum, not a closed set.
 */
export function validateToolPackage(value: unknown): string[] {
  const errs: string[] = [];
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return ["tool package must be a JSON object"];
  }
  const t = value as Record<string, unknown>;

  // name: kebab-case, non-empty
  if (typeof t.name !== "string" || t.name.length === 0) {
    errs.push("name: required non-empty string");
  } else if (!KEBAB.test(t.name)) {
    errs.push(`name: must be kebab-case (got ${JSON.stringify(t.name)})`);
  }

  // version: semver-shaped string
  if (typeof t.version !== "string") {
    errs.push("version: required string");
  } else if (!SEMVER.test(t.version)) {
    errs.push(`version: must look like semver X.Y.Z (got ${JSON.stringify(t.version)})`);
  }

  // description: non-empty string
  if (typeof t.description !== "string" || t.description.trim().length === 0) {
    errs.push("description: required non-empty string");
  }

  // kind: "script" | "appbundle"
  if (typeof t.kind !== "string" || !VALID_KINDS.has(t.kind)) {
    errs.push(`kind: must be one of ${[...VALID_KINDS].join("|")}`);
  }

  // engine_op: non-empty string
  if (typeof t.engine_op !== "string" || t.engine_op.length === 0) {
    errs.push("engine_op: required non-empty string");
  }

  // params: JSON Schema object with type:"object"
  if (typeof t.params !== "object" || t.params === null || Array.isArray(t.params)) {
    errs.push("params: required JSON Schema object");
  } else {
    const p = t.params as Record<string, unknown>;
    if (p.type !== "object") errs.push('params.type: must be "object"');
    if (p.properties !== undefined && (typeof p.properties !== "object" || p.properties === null)) {
      errs.push("params.properties: must be an object when present");
    }
    if (p.required !== undefined && !Array.isArray(p.required)) {
      errs.push("params.required: must be an array when present");
    }
  }

  // returns: object
  if (typeof t.returns !== "object" || t.returns === null || Array.isArray(t.returns)) {
    errs.push("returns: required JSON Schema object");
  }

  // capabilities: array subset of {drawing.read, drawing.write}
  if (!Array.isArray(t.capabilities)) {
    errs.push("capabilities: required array");
  } else {
    for (const c of t.capabilities) {
      if (typeof c !== "string" || !VALID_CAPS.has(c)) {
        errs.push(`capabilities: unknown capability ${JSON.stringify(c)}`);
      }
    }
  }

  // provenance: { author: agent|user, created: iso8601 }
  if (typeof t.provenance !== "object" || t.provenance === null || Array.isArray(t.provenance)) {
    errs.push("provenance: required object {author, created}");
  } else {
    const pr = t.provenance as Record<string, unknown>;
    if (typeof pr.author !== "string" || !VALID_AUTHORS.has(pr.author)) {
      errs.push(`provenance.author: must be one of ${[...VALID_AUTHORS].join("|")}`);
    }
    if (typeof pr.created !== "string" || !ISO8601.test(pr.created)) {
      errs.push("provenance.created: must be an ISO-8601 timestamp");
    }
  }

  return errs;
}

/** True iff the value satisfies the frozen CONTRACT section 2 schema. */
export function isValidToolPackage(value: unknown): value is ToolPackage {
  return validateToolPackage(value).length === 0;
}
