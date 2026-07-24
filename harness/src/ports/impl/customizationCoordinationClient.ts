/**
 * Protected app callbacks for the customization lifecycle. The harness sends
 * immutable receipts only. Prompts, source, grants, and dispatch secrets never
 * cross this boundary or reach logs.
 */

import type { CustomizationCoordination, StagedCustomizationReceipt } from "../index.js";

export interface CustomizationCoordinationClientOptions {
  baseUrl: string;
  dispatchSecret: string;
}

export class CustomizationCoordinationClient implements CustomizationCoordination {
  private readonly baseUrl: string;
  private readonly dispatchSecret: string;

  constructor(opts: CustomizationCoordinationClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.dispatchSecret = opts.dispatchSecret;
  }

  private async post(path: string, tenantId: string, body: unknown): Promise<void> {
    if (!this.baseUrl || !this.dispatchSecret) {
      throw new Error("customization coordination is not configured");
    }
    let response: Response;
    try {
      response = await fetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-tenant-id": tenantId,
          "x-dispatch-secret": this.dispatchSecret,
        },
        body: JSON.stringify(body),
      });
    } catch {
      throw new Error("customization coordination is unavailable");
    }
    if (!response.ok) {
      // Do not include a callback body in this error. It may contain internal
      // diagnostics, and the harness's request-error logger is intentionally broad.
      throw new Error(`customization coordination rejected request (${response.status})`);
    }
  }

  async recordStaged(receipt: StagedCustomizationReceipt): Promise<void> {
    await this.post("/internal/customization/staged", receipt.tenant_id, { receipt });
  }

  async authorizePublish(receipt: StagedCustomizationReceipt, expectedMainSha: string): Promise<void> {
    await this.post("/internal/customization/authorize-publish", receipt.tenant_id, {
      receipt,
      expected_main_sha: expectedMainSha,
    });
  }
}
