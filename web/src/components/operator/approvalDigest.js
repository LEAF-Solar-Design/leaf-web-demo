/**
 * Approval digest — normalizes a runbook `propose` response (plus the
 * session's server-resolved environment) into the fixed field set
 * ApprovalCard renders (Wave 2 Lane E acceptance #3): target, environment,
 * cost, scope, expiry, reversal, argument digest.
 *
 * EVERY field must trace to server truth: either the propose response body
 * itself, or `sessionEnvironment`, which the console reads from the
 * session's own GET (operator_session_store rows — server-resolved via
 * require_operator, never a client guess). Nothing here invents a default:
 * a field this module cannot find on the response is left `undefined`, and
 * ApprovalCard refuses to render an Execute control while any REQUIRED_FIELD
 * is undefined (fail closed, not fail open).
 *
 * KNOWN GAP (flagged in the PR as a server follow-up, not fixed here — out of
 * scope for a web-only lane): as of this landing, no mounted `propose` route
 * returns `args_hash` (operator_authority.mint computes it but the runbook
 * propose() functions do not forward it), and only tenant-agent pause/resume
 * returns a `reversal` description at propose time (the others only include
 * it on the EXECUTE response, too late to show before approval). Until the
 * backend adds those fields, this normalizer honestly reports them missing
 * and the card blocks Execute — the correct behavior for a card whose whole
 * job is to never render a control from a guessed value.
 */

export const REQUIRED_FIELDS = ['target', 'environment', 'cost', 'scope', 'expiry', 'reversal', 'argsDigest']

const FIELD_LABELS = {
  target: 'target',
  environment: 'environment',
  cost: 'cost',
  scope: 'scope',
  expiry: 'expiry',
  reversal: 'reversal',
  argsDigest: 'argument digest',
}

export function fieldLabel(field) {
  return FIELD_LABELS[field] || field
}

function firstDefined(...values) {
  for (const v of values) {
    if (v !== undefined && v !== null && v !== '') return v
  }
  return undefined
}

function readTarget(response) {
  if (response.tenant_id) return `tenant:${response.tenant_id}`
  if (response.credential_handle) return `credential:${response.credential_handle}`
  if (response.destination) return `destination:${response.destination}`
  if (response.source_sha && response.target) return `${response.target}:${response.source_sha}`
  return firstDefined(response.target)
}

function readReversal(response) {
  if (typeof response.reversal_action === 'string') return response.reversal_action
  if (response.reversal && typeof response.reversal === 'object') {
    const [key, value] = Object.entries(response.reversal)[0] || []
    return key ? `${key}: ${value}` : undefined
  }
  return undefined
}

function readCost(response) {
  if (typeof response.cost === 'string') return response.cost
  if (Number.isFinite(response.spend_cents)) return `$${(response.spend_cents / 100).toFixed(2)}`
  if (Number.isFinite(response.max_spend_cents)) return `up to $${(response.max_spend_cents / 100).toFixed(2)}`
  return undefined
}

/**
 * `response` is a raw propose() body from operatorClient.js. `sessionEnvironment`
 * is the OperatorContext.environment the console read from the session GET —
 * used ONLY as a fallback when the propose response itself omits `environment`
 * (credential rotate and external_write DO include it today; the rest don't).
 */
export function normalizeApproval(response, sessionEnvironment) {
  if (!response || typeof response !== 'object') {
    return { action: undefined, authorityId: undefined, digest: {}, missing: [...REQUIRED_FIELDS] }
  }
  const digest = {
    target: readTarget(response),
    environment: firstDefined(response.environment, sessionEnvironment),
    cost: readCost(response),
    scope: firstDefined(response.scope),
    expiry: firstDefined(response.expires_at),
    reversal: readReversal(response),
    argsDigest: firstDefined(response.args_hash),
  }
  const missing = REQUIRED_FIELDS.filter((f) => digest[f] === undefined)
  return {
    action: response.action,
    authorityId: response.authority_id,
    digest,
    missing,
  }
}
