import { AsyncLocalStorage } from "node:async_hooks";
import { createHash, randomUUID } from "node:crypto";
import type { AgentRunInput, AgentRunResult, AgentRunner } from "../index.js";

export interface CostEvent {
  org_id: string;
  addon: "e2b_exec";
  mode: "managed";
  vendor: "e2b";
  unit: "sandbox_seconds";
  quantity: string;
  unit_cost_usd: string;
  cost_usd: string;
  cost_basis: "estimated";
  event_key: string;
  occurred_at: string;
  provenance: { source: "harness-e2b"; run_ref: string };
}

export interface CostEventSink {
  record(event: CostEvent): Promise<void>;
}

export type MeteringConfig = { enabled: true; rate: string } | { enabled: false; reason: string };

export function meteringConfigFromEnv(env: NodeJS.ProcessEnv): MeteringConfig {
  if (env.LEAF_COST_METERING !== "1") {
    return { enabled: false, reason: 'LEAF_COST_METERING must equal "1"' };
  }
  const rate = env.LEAF_E2B_USD_PER_SECOND;
  if (!rate || rate.length > 12 || !/^\d+(?:\.\d+)?$/.test(rate) || !/[1-9]/.test(rate)) {
    return { enabled: false, reason: "LEAF_E2B_USD_PER_SECOND must be a positive decimal string of at most 12 characters" };
  }
  return { enabled: true, rate };
}

export interface MeteringIdentity { orgId?: string; runRef?: string }
const identity = new AsyncLocalStorage<MeteringIdentity>();

/** Only the authenticated platform request boundary may supply this identity. */
export function withMeteringIdentity<T>(value: MeteringIdentity, action: () => Promise<T>): Promise<T> {
  return identity.run(value, action);
}

export function e2bCostEventKey(orgId: string, runRef: string): string {
  if (!runRef || !/^[\x00-\x7f]+$/.test(runRef)) throw new Error("run_ref must be nonempty ASCII");
  const canonical = JSON.stringify([
    "leaf-tenant-cost:v1", orgId.toLowerCase(), "e2b_exec", "harness-e2b", runRef,
  ]).replace(/\x7f/g, "\\u007f"); // Python ensure_ascii also escapes ASCII DEL.
  return createHash("sha256").update(canonical).digest("hex");
}

/** Milliseconds are the quantity's exact thousandths; round micro-dollars half-up. */
export function e2bCostUsd(milliseconds: bigint, rate: string): string {
  const [whole, fraction = ""] = rate.split(".");
  const numerator = milliseconds * BigInt(whole + fraction) * 1000000n;
  const denominator = 1000n * 10n ** BigInt(fraction.length);
  const micros = (numerator + denominator / 2n) / denominator;
  return `${micros / 1000000n}.${(micros % 1000000n).toString().padStart(6, "0")}`;
}

export interface MeteredAgentRunnerOptions {
  rate: string;
  sink: CostEventSink;
  identityFor?: (input: AgentRunInput) => MeteringIdentity | undefined;
  monotonicNow?: () => bigint;
  endTime?: () => Date;
  log?: (message: string) => void;
}

export class MeteredAgentRunner implements AgentRunner {
  readonly counters = { skipped_unresolved: 0 };
  private readonly logged = new Set<string>();

  constructor(private readonly inner: AgentRunner, private readonly options: MeteredAgentRunnerOptions) {}

  private logOnce(category: string): void {
    if (this.logged.has(category)) return;
    this.logged.add(category);
    // Even an injected logger must not affect the runner's outcome. No error text or secrets.
    try { (this.options.log ?? console.warn)(`[cost-metering] ${category}`); } catch {}
  }

  async run(input: AgentRunInput): Promise<AgentRunResult> {
    const now = this.options.monotonicNow ?? process.hrtime.bigint;
    const start = now();
    try {
      return await this.inner.run(input);
    } finally {
      try {
        const end = now();
        const occurredAt = (this.options.endTime ?? (() => new Date()))().toISOString();
        const context = this.options.identityFor ? this.options.identityFor(input) : identity.getStore();
        if (!context?.orgId || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(context.orgId)) {
          this.counters.skipped_unresolved++;
          this.logOnce("skipped_unresolved");
        } else {
          const milliseconds = (end - start + 500000n) / 1000000n;
          // AgentRunInput has no id, so assign this execution its own run id.
          const runRef = context.runRef ?? randomUUID();
          const orgId = context.orgId.toLowerCase();
          await this.options.sink.record({
            org_id: orgId, addon: "e2b_exec", mode: "managed", vendor: "e2b",
            unit: "sandbox_seconds",
            quantity: `${milliseconds / 1000n}.${(milliseconds % 1000n).toString().padStart(3, "0")}`,
            unit_cost_usd: this.options.rate,
            cost_usd: e2bCostUsd(milliseconds, this.options.rate), cost_basis: "estimated",
            event_key: e2bCostEventKey(orgId, runRef), occurred_at: occurredAt,
            provenance: { source: "harness-e2b", run_ref: runRef },
          });
        }
      } catch (error) {
        this.logOnce(`sink_failure:${error instanceof Error ? error.name : typeof error}`);
      }
    }
  }
}
