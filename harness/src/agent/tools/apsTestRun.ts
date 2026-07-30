/**
 * `apsTestRun` author tool - delegates to the BrokerApsClient (broker ONLY) to
 * test-run a candidate tool. Always `apsLive: false` (a design-time test run uses
 * the broker's pure-python mock path; the harness never touches the APS
 * credential - only the broker process holds it, per CONTRACT-ADDENDUM section 8).
 */

import type { BrokerApsClient, ResultEnvelope, ToolPackage } from "../../ports/index.js";

/** Bind an apsTestRun function to a broker client + tenant. */
export function makeApsTestRun(
  broker: BrokerApsClient,
  tenantId: string,
  dwg = "rooftop_demo",
): (
  tool: ToolPackage,
  params?: Record<string, unknown>,
  testSource?: string,
) => Promise<ResultEnvelope> {
  return (tool, params = {}, testSource) => {
    const capabilities = Array.isArray(tool.capabilities) ? tool.capabilities : [];
    const safeParams = capabilities.includes("drawing.write")
      ? {
          ...params,
          ...(params.drawing_id == null ? { drawing_id: dwg } : {}),
          dry_run: true,
        }
      : params;
    return broker.runTool({
      tenantId,
      tool,
      params: safeParams,
      dwg,
      apsLive: false,
      ...(testSource === undefined ? {} : { testSource }),
    });
  };
}
