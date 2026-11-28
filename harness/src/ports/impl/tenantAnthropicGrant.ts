import { randomUUID } from "node:crypto";
import type { AgentGrant, GrantLease, OAuthGrantProvider } from "../index.js";
import {
  FileTenantProviderStore, allowedProviderHosts, parseTenantProviderOverride, TenantProviderConfigError,
} from "./tenantProviderStore.js";

export type ProjectAuthoringRoute = {
  route_class: "approved_subscription_pool" | "tenant_override" | "paid_or_mixed_pool" | "none" | "unknown";
  eligible: boolean;
  reason: "ready" | "override_present" | "paid_route_selectable" | "no_eligible_grant" | "metadata_unavailable";
};

/** Per-tenant SDK routing; credentials are resolved only on the trusted harness host. */
export class TenantAnthropicGrantProvider implements OAuthGrantProvider {
  private readonly store: FileTenantProviderStore;
  readonly acquireGrant?: OAuthGrantProvider["acquireGrant"];
  readonly settleGrant?: OAuthGrantProvider["settleGrant"];

  constructor(
    private readonly inner: OAuthGrantProvider,
    grantsDir: string,
    private readonly env: NodeJS.ProcessEnv = process.env,
  ) {
    this.store = new FileTenantProviderStore(grantsDir);
    // Chat turns take a lease (acquireGrant) when the inner provider pools grants; a tenant with an
    // anthropic_compatible record must get its own route on that path too, never a pooled grant.
    if (inner.acquireGrant) {
      this.acquireGrant = async (tenantId: string): Promise<GrantLease> => {
        const own = await this.routedGrant(tenantId);
        if (own) return { grant: own, account_id: "tenant-provider", lease_id: `tenant-provider:${randomUUID()}` };
        return inner.acquireGrant!(tenantId);
      };
    }
    if (inner.settleGrant) {
      this.settleGrant = async (tenantId, leaseId, outcome) => {
        if (leaseId.startsWith("tenant-provider:")) return;
        return inner.settleGrant!(tenantId, leaseId, outcome);
      };
    }
  }

  async getGrant(tenantId: string): Promise<AgentGrant> {
    return (await this.routedGrant(tenantId)) ?? this.inner.getGrant(tenantId);
  }

  async observeProjectAuthoringRoute(
    tenantId: string, selectableKinds: () => Promise<readonly AgentGrant["kind"][] | null>,
  ): Promise<ProjectAuthoringRoute> {
    const unknown = { route_class: "unknown", eligible: false, reason: "metadata_unavailable" } as const;
    try {
      if (await this.anthropicOverride(tenantId)) {
        return { route_class: "tenant_override", eligible: false, reason: "override_present" };
      }
      const selectable = await selectableKinds();
      if (selectable === null) return unknown;
      if (selectable.includes("api_key")) {
        return { route_class: "paid_or_mixed_pool", eligible: false, reason: "paid_route_selectable" };
      }
      return selectable.includes("oauth")
        ? { route_class: "approved_subscription_pool", eligible: true, reason: "ready" }
        : { route_class: "none", eligible: false, reason: "no_eligible_grant" };
    } catch {
      return unknown;
    }
  }

  private async anthropicOverride(tenantId: string) {
    const raw = await this.store.get(tenantId);
    if (raw === null) return null;
    const override = parseTenantProviderOverride(raw, allowedProviderHosts(this.env));
    return override.provider === "anthropic_compatible" ? override : null;
  }

  /** The tenant's anthropic_compatible grant, or null when it has none (fails closed on a bad record). */
  private async routedGrant(tenantId: string): Promise<AgentGrant | null> {
    const override = await this.anthropicOverride(tenantId);
    if (override === null) return null;
    const apiKey = this.env[override.api_key_env]?.trim();
    if (!apiKey) throw new TenantProviderConfigError("provider key unavailable");
    return { kind: "api_key", apiKey, baseUrl: override.base_url, model: override.model };
  }
}
