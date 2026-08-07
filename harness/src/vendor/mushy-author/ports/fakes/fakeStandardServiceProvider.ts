import type {
  StandardServiceCall,
  StandardServiceCatalog,
  StandardServiceIdentity,
  StandardServiceProvider,
  StandardServiceRequestResult,
  StandardServiceResult,
  StandardServiceStatus,
  StandardServiceVisualResult,
} from "../impl/standardServices.js";
import {
  STANDARD_SERVICE_CATALOG_V1,
  tenantBrokerApprovalDigest,
  standardServiceCatalogDigest,
} from "../impl/standardServices.js";

export interface FakeStandardServiceProviderOptions {
  catalog?: StandardServiceCatalog;
  deny?: (identity: StandardServiceIdentity, operation: string) => string | null;
}

/** Contract-faithful fake. It binds approvals to identity and exact arguments. */
export class FakeStandardServiceProvider implements StandardServiceProvider {
  private readonly serviceCatalog: StandardServiceCatalog;
  private readonly deny?: FakeStandardServiceProviderOptions["deny"];
  private readonly approvals = new Map<string, {
    identity: StandardServiceIdentity;
    call: StandardServiceCall;
    approved: boolean;
    used: boolean;
  }>();
  private nextApproval = 1;

  constructor(options: FakeStandardServiceProviderOptions = {}) {
    this.serviceCatalog = options.catalog ?? STANDARD_SERVICE_CATALOG_V1;
    this.deny = options.deny;
  }

  private assertAllowed(identity: StandardServiceIdentity, operation: string): void {
    for (const value of [
      identity.tenant_id,
      identity.subject_id,
      identity.session_id,
      identity.authority_turn_id,
      identity.subscription_mount_id,
      identity.runner_profile_id,
    ]) {
      if (!value) throw new Error("standard_service_identity_incomplete");
    }
    const reason = this.deny?.(identity, operation);
    if (reason) throw new Error(`standard_service_denied:${reason}`);
  }

  async catalog(identity: StandardServiceIdentity): Promise<StandardServiceCatalog> {
    this.assertAllowed(identity, "catalog");
    return structuredClone(this.serviceCatalog);
  }

  async read(identity: StandardServiceIdentity, call: StandardServiceCall): Promise<StandardServiceResult> {
    this.assertAllowed(identity, `read:${call.service_id}/${call.tool_id}`);
    return { content: JSON.stringify({ call }) };
  }

  async request(identity: StandardServiceIdentity, call: StandardServiceCall): Promise<StandardServiceRequestResult> {
    this.assertAllowed(identity, `request:${call.service_id}/${call.tool_id}`);
    const approvalId = `approval-${this.nextApproval++}`;
    this.approvals.set(approvalId, {
      identity: structuredClone(identity),
      call: structuredClone(call),
      approved: false,
      used: false,
    });
    return {
      approval_id: approvalId,
      argument_digest: tenantBrokerApprovalDigest(identity, call),
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      summary: `${call.service_id}/${call.tool_id}`,
    };
  }

  /** Simulate a separate authenticated human approval action. Never model-mounted. */
  approve(approvalId: string): void {
    const approval = this.approvals.get(approvalId);
    if (!approval || approval.used) throw new Error("standard_service_approval_invalid_or_used");
    approval.approved = true;
  }

  async confirm(identity: StandardServiceIdentity, approvalId: string): Promise<StandardServiceResult> {
    this.assertAllowed(identity, "confirm");
    const approval = this.approvals.get(approvalId);
    if (!approval || approval.used) throw new Error("standard_service_approval_invalid_or_used");
    if (JSON.stringify(approval.identity) !== JSON.stringify(identity)) {
      throw new Error("standard_service_approval_identity_mismatch");
    }
    if (!approval.approved) throw new Error("standard_service_approval_pending_human");
    approval.used = true;
    return { content: JSON.stringify({ call: approval.call }), receipt_id: `receipt-${approvalId}` };
  }

  async visualInspect(
    identity: StandardServiceIdentity,
    inspectionTargetId: string,
    viewport: "desktop" | "mobile",
  ): Promise<StandardServiceVisualResult> {
    this.assertAllowed(identity, "visual:inspect-issued-target");
    if (!/^[A-Za-z0-9_-]{8,256}$/.test(inspectionTargetId)) {
      throw new Error("standard_service_visual_target_must_be_issued_id");
    }
    return {
      inspection_target_id: inspectionTargetId,
      content: JSON.stringify({ viewport, dom: "fake", console_errors: [] }),
      image_artifact_id: `image-${inspectionTargetId}-${viewport}`,
      media_type: "image/png",
    };
  }

  async status(identity: StandardServiceIdentity): Promise<StandardServiceStatus> {
    this.assertAllowed(identity, "status");
    return {
      state: "ready",
      catalog_digest: standardServiceCatalogDigest(this.serviceCatalog),
      services: { visual: "ready" },
    };
  }
}
