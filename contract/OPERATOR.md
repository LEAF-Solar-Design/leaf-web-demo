# Operator principal contract

## §1 The operator_principals roster

`operator_principals` (schema: `platform/migrations/0020_operator_principals.sql`)
is the durable roster of human operator principals and the revision anchor for
any operator-scoped authority the platform issues.

Rules:

1. **Single writer.** The ONLY writer is `scripts/operator_principal_admin.py`,
   run out-of-band with a direct `DATABASE_URL`. The app never writes this
   table. Any other write path is a contract violation.
2. **Revision-bound authority.** Every mutation bumps `role_revision`. Any
   approval, session grant, or execution authority minted for a principal MUST
   capture the `role_revision` current at mint time and MUST deny at redemption
   when the row's current revision differs. Suspending, revoking, or re-granting
   a principal therefore invalidates everything minted before it.
3. **Status transitions are guarded.**
   - `grant` creates an `active` row, or re-activates an existing row **only**
     when the operator passes `--reactivate` (a revoked or suspended principal
     is never resurrected by a routine grant).
   - `suspend` applies only to `active` rows.
   - `resume` applies only to `suspended` rows — it can never un-revoke.
   - `revoke` is terminal; only `grant --reactivate` (a deliberate, flagged act)
     brings a revoked principal back.
4. **Attributed grants.** `granted_by` is required (schema `NOT NULL`): every
   grant names the accountable human who authorized it. Re-grants never erase
   the attribution — they replace it with the new grantor.
5. **Explicit scope updates only.** A re-grant changes `profiles` /
   `environment` only when those flags are passed explicitly; a bare
   `grant <subject>` never silently rewrites an existing row's scope back to
   defaults.
