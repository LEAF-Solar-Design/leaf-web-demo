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
): (tool: ToolPackage, params?: Record<string, unknown>) => Promise<ResultEnvelope> {
  return (tool, params = {}) =>
    broker.runTool({ tenantId, tool, params, dwg, apsLive: false });
}
