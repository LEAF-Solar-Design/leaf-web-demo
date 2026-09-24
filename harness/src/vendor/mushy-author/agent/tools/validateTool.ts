/**
 * `validateTool` author tool - runs the CONTRACT.md section 2 oracle against a
 * candidate tool package and returns pass/fail + diagnostics. The design-time
 * session calls this before returning; the harness ALSO re-runs it after the
 * session (defense in depth) before registering.
 *
 * For `view` packages the same call also runs the fragment checks (page
 * skeleton, position:fixed, hardcoded colours, external origins) when the
 * entry source is supplied — one validator, both halves.
 */

import type { ToolPackage, ValidationResult } from "../../ports/index.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";
import { checkViewFragment } from "../../registry/viewChecks.js";

export function validateTool(tool: ToolPackage, source?: string): ValidationResult {
  const diagnostics = validateToolPackage(tool);
  if (tool.kind === "view" && typeof source === "string") {
    diagnostics.push(...checkViewFragment(source));
  }
  return { ok: diagnostics.length === 0, diagnostics };
}
