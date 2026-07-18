/**
 * `validateTool` author tool - runs the CONTRACT.md section 2 oracle against a
 * candidate tool package and returns pass/fail + diagnostics. The design-time
 * session calls this before returning; the harness ALSO re-runs it after the
 * session (defense in depth) before registering.
 */

import type { ToolPackage, ValidationResult } from "../../ports/index.js";
import { validateToolPackage } from "../../registry/toolPackageSchema.js";

export function validateTool(tool: ToolPackage): ValidationResult {
  const diagnostics = validateToolPackage(tool);
  return { ok: diagnostics.length === 0, diagnostics };
}
