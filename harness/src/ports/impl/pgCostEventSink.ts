import type { Pool } from "pg";
import type { CostEvent, CostEventSink } from "./meteredAgentRunner.js";

export const COST_EVENT_STATEMENT_TIMEOUT_MS = 5000;

export class CostEventConflict extends Error {
  override name = "CostEventConflict";
}

/** Borrows the harness's pg pool; its owner remains responsible for closing it. */
export class PgCostEventSink implements CostEventSink {
  constructor(private readonly pool: Pick<Pool, "connect">) {}

  async record(event: CostEvent): Promise<void> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN ISOLATION LEVEL READ COMMITTED");
      await client.query("SELECT set_config('statement_timeout', $1, true)", [String(COST_EVENT_STATEMENT_TIMEOUT_MS)]);
      const values = [event.org_id, event.addon, event.mode, event.vendor, event.unit,
        event.quantity, event.unit_cost_usd, event.cost_usd, event.cost_basis,
        event.event_key, event.occurred_at, JSON.stringify(event.provenance)];
      const inserted = await client.query(
        `INSERT INTO tenant_cost_events
          (org_id, addon, mode, vendor, unit, quantity, unit_cost_usd, cost_usd,
           cost_basis, event_key, occurred_at, provenance)
         VALUES ($1::uuid, $2, $3, $4, $5, $6::numeric, $7::numeric, $8::numeric,
                 $9, $10, $11::timestamptz, $12::jsonb)
         ON CONFLICT (event_key) DO NOTHING RETURNING id`, values,
      );
      if (!inserted.rows.length) {
        // Separate statement sees a competing committed insert under READ COMMITTED.
        const existing = await client.query(
          `SELECT id FROM tenant_cost_events WHERE
           org_id IS NOT DISTINCT FROM $1::uuid AND addon IS NOT DISTINCT FROM $2
           AND mode IS NOT DISTINCT FROM $3 AND vendor IS NOT DISTINCT FROM $4
           AND unit IS NOT DISTINCT FROM $5 AND quantity IS NOT DISTINCT FROM $6::numeric
           AND unit_cost_usd IS NOT DISTINCT FROM $7::numeric AND cost_usd IS NOT DISTINCT FROM $8::numeric
           AND cost_basis IS NOT DISTINCT FROM $9 AND event_key IS NOT DISTINCT FROM $10
           AND occurred_at IS NOT DISTINCT FROM $11::timestamptz AND provenance IS NOT DISTINCT FROM $12::jsonb`,
          values,
        );
        if (!existing.rows.length) throw new CostEventConflict("event_key was replayed with different values");
      }
      await client.query("COMMIT");
    } catch (error) {
      try { await client.query("ROLLBACK"); } catch { /* Preserve the original failure. */ }
      throw error;
    } finally {
      client.release();
    }
  }
}
