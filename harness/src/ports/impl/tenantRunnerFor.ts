// The one per-tenant runner factory both harness entrypoints (src/server.ts startReal and the
// container entrypoint scripts/serve.ts) hand to SpineTurnAdapter as `tenantRunnerFor`.
//
// Default off: a tenant with no `<grantsDir>/<tenant>.provider.json` record resolves to null and
// keeps the entrypoint's normal runner. A record that cannot be honoured (host not allowlisted,
// key variable missing, malformed) throws a value-free error so the turn fails closed and never
// silently falls back to another provider. See tenantProviderStore.ts for the record contract.
import type { StandardServicesResolver } from "../../vendor/mushy-author/index.js";
import type { AgentGrant, SpineConverseRunner } from "../index.js";
import { OpenAiConverseRunner } from "./openaiConverseRunner.js";
import { FileTenantProviderStore, resolveTenantProvider } from "./tenantProviderStore.js";

export type TenantRunnerFor = (tenantId: string, grant: AgentGrant) =>
  Promise<{ runner: SpineConverseRunner; secrets: string[] } | null>;

export interface TenantRunnerForOptions {
  /** The grant directory; provider records live beside the tenant's grant files. */
  grantsDir?: string;
  /** Injectable store (tests); defaults to the file store under grantsDir. */
  store?: Pick<FileTenantProviderStore, "get">;
  env?: NodeJS.ProcessEnv;
  standardServicesResolver?: StandardServicesResolver;
}

export function createTenantRunnerFor(options: TenantRunnerForOptions): TenantRunnerFor {
  const store = options.store ?? new FileTenantProviderStore(options.grantsDir ?? "");
  if (!options.store && !options.grantsDir) throw new Error("tenant runner factory needs a grants dir");
  const env = options.env ?? process.env;
  const { standardServicesResolver } = options;
  return async (tenantId, grant) => {
    const resolved = await resolveTenantProvider(store, tenantId, env);
    if (!resolved) return null;
    const { override, apiKey } = resolved;
    return {
      secrets: [apiKey],
      runner: new OpenAiConverseRunner({
        grant, apiKey, model: override.model, baseUrl: override.base_url,
        identity: { provider: "openai_compatible", label: override.label },
        ...(override.turn_timeout_s !== undefined ? { turnTimeoutS: override.turn_timeout_s } : {}),
        ...(standardServicesResolver ? { standardServicesResolver } : {}),
      }),
    };
  };
}
