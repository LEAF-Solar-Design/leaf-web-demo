// ---------------------------------------------------------------------------
// THE BUILD QUEUE RECORD (standardization slice 11a, docs/convergence/
// SURFACE-CONTRACT.md `builds.card`). ONE shape for every build the product
// can show, whatever ran it:
//
//   lane 'broker'  a one-shot tool run through the jobs store (GET /api/jobs,
//                  useJobController rows). JobRail's vocabulary is preserved
//                  verbatim: running / submitted / spend cap / plan / failed /
//                  degraded / complete, with the same cost formatting and the
//                  same calm (amber) treatment for quota and plan rejections.
//   lane 'fold'    a multi-round autonomous run read off its
//                  durable state file: rounds, spent_usd, mission_complete,
//                  unverified_complete, escalated, per-milestone verified_at.
//   lane 'fleet'   a task the fleet collector tracks: task_state.state plus
//                  the gateway's own `requested_by` stamp when it sends one
//                  (slice 11b); `null` until then. `tasks.owner` names who is
//                  EXECUTING the task, never who asked for it, so it is never
//                  substituted in.
//
// TWO-STAGE TERMINAL, never inferred. `terminal.verified` is true only when
// the lane's OWN terminal artifact exists: a broker job's own terminal
// receipt (kind 'terminal', written beside the completed job), a multi-round
// run's milestone verified_at under a real oracle, or a gate-proof/
// verification receipt on a fleet task. A bare 'done'/'complete' status word
// is NOT evidence on its own, on any lane: the wire validator enforces this
// (a record cannot claim verified:true without one of those receipt kinds
// present, the same way it already refuses promoted:true with no promotion
// receipt). `terminal.promoted` is true only when a PROMOTION artifact exists
// (the prewarm cutover receipt `leaf.staging-prewarm-relay.v1`, an App Store
// Connect result carrying a build id, a janitor promotion stage), carried as
// a receipt of kind 'promotion'. Neither flag is ever derived from elapsed
// time, from a status word, or from the other flag.
//
// PURE: no React, no fetch, no clock, no locale. Time-relative text ("5 m",
// a clock) is the card's job, because it depends on Date.now().
//
// HARDENING CONTRACT. Every string is bounded (BUILD_LIMITS), every number is
// finite and non-negative, every enum is closed. `parseBuildRecord` FAILS
// CLOSED: a malformed record yields { ok: false, reason } and never throws,
// never repairs, never guesses a lane or a state. The mappers produce records
// that pass it by construction (pinned by buildQueue.test.js against the
// shared cases in contract/build-queue.v1.cases.json, which the server's
// mirror server/build_queue.py is pinned against too), and a source row that
// cannot be mapped honestly (an unknown state, no id) maps to null so the
// caller can COUNT the drop instead of rendering a guess.
// ---------------------------------------------------------------------------

export const BUILD_LANES = Object.freeze(['fold', 'broker', 'fleet'])
export const BUILD_STATES = Object.freeze(['queued', 'running', 'verifying', 'done', 'failed'])
export const BUILD_ACTIONS = Object.freeze(['cancel', 'retry', 'promote'])
export const RECEIPT_KINDS = Object.freeze(['terminal', 'verification', 'promotion', 'artifact', 'gate-proof'])
export const STATUS_TINTS = Object.freeze(['ok', 'warn', 'err', 'mut'])

// Bounds. `id` and `ref` are identifiers (a job id, an artifact path, a run
// id); `text` is anything a person reads (a title, a status word, a detail).
export const BUILD_LIMITS = Object.freeze({ id: 128, text: 200, ref: 512, receipts: 32, records: 200 })

// The promotion artifacts this record recognises, by name. A promotion is a
// FACT carried by one of these, never a state the mapper reaches on its own.
export const PROMOTION_ARTIFACTS = Object.freeze({
  prewarmRelaySchema: 'leaf.staging-prewarm-relay.v1',
  appStoreConnectStatuses: Object.freeze(['succeeded', 'processed', 'valid', 'accepted']),
  janitorPromotedStatuses: Object.freeze(['promoted', 'done', 'complete']),
})

const LANE_SET = new Set(BUILD_LANES)
const STATE_SET = new Set(BUILD_STATES)
const ACTION_SET = new Set(BUILD_ACTIONS)
const RECEIPT_KIND_SET = new Set(RECEIPT_KINDS)
const TINT_SET = new Set(STATUS_TINTS)
const OPEN_STATES = new Set(['queued', 'running', 'verifying'])

// --- small guards ----------------------------------------------------------

const isPlainObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v)

/** A non-empty string within `max` chars, else null. Never trims: a source
 *  that pads its ids is a source whose ids are padded. */
function boundedString(value, max) {
  if (typeof value !== 'string' || value.length === 0 || value.length > max) return null
  return value
}

/** Text a person reads: clipped to the bound with an ellipsis. A mapper's
 *  presentation bound, not a security one (ids never go through here). */
function clip(value, max = BUILD_LIMITS.text) {
  if (typeof value !== 'string' || value.length === 0) return null
  return value.length > max ? `${value.slice(0, max - 1)}…` : value
}

/** A finite number >= 0, else null. Booleans are not numbers. */
function nonNegative(value) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null
  return value
}

/** A finite integer >= 0, else null (a float that IS integral rounds to itself). */
function nonNegativeInt(value) {
  const n = nonNegative(value)
  return n === null ? null : Math.round(n)
}

/**
 * Epoch milliseconds from the shapes the sources use: epoch seconds (the job
 * store's REAL columns), epoch milliseconds, or an ISO-8601 string. Null for
 * anything else. The 1e12 split is JobRail's (fmtWhen): 1e12 ms is 2001, and
 * no epoch-seconds value this product will ever see reaches it.
 */
export function toEpochMs(value) {
  if (typeof value === 'number') {
    if (!Number.isFinite(value) || value <= 0) return null
    return Math.round(value < 1e12 ? value * 1000 : value)
  }
  if (typeof value === 'string' && value.length > 0 && value.length <= 64) {
    const t = Date.parse(value)
    return Number.isFinite(t) && t > 0 ? t : null
  }
  return null
}

// --- JobRail's own vocabulary, moved here verbatim so the broker mapper and
// the rail can never disagree about a status word or a cost string ---------

/** A quota rejection (broker hard cap) rides in as a failed job whose
 *  error_code is 'quota_exceeded'. An expected budget state, not an alarm. */
export function isQuotaRejection(job) {
  const c = (job.error && job.error.error_code) || job.error_code
  return job.status === 'failed' && c === 'quota_exceeded'
}

/** An entitlement rejection (a write tool the plan doesn't include) is also
 *  a failed job, carried as `entitlement_required`. */
export function isEntitlementRejection(job) {
  return job.status === 'failed' && !!job.entitlement_required
}

/** Per-run cost text. Null for no cost AND for a zero cost, so a row shows
 *  just the clock, never "$0.0000" (JobRail B1). */
export function formatCostUsd(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return null
  return n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`
}

/** The job's own cost or its stored envelope's, as a number > 0, else null. */
export function brokerCostUsd(job) {
  const c = job.cost || (job.result && job.result.cost)
  const n = Number(c && c.usd_est)
  return Number.isFinite(n) && n > 0 ? n : null
}

/**
 * Status word (sentence case, the app's calm vocabulary) + which tint voice
 * it speaks: ok (green), warn (amber), err (red), mut (neutral, hollow states
 * keep neutral words). Byte-for-byte JobRail's stateTag.
 */
export function brokerStateTag(job) {
  if (job.status === 'running') return { tint: 'ok', label: 'running' }
  if (job.status === 'submitted') return { tint: 'mut', label: 'submitted' }
  if (job.status === 'failed') {
    if (isQuotaRejection(job)) return { tint: 'warn', label: 'spend cap' }
    if (isEntitlementRejection(job)) return { tint: 'warn', label: 'plan' }
    return { tint: 'err', label: 'failed' }
  }
  if (job.status === 'complete') {
    return job.degraded_mode ? { tint: 'warn', label: 'degraded' } : { tint: 'ok', label: 'complete' }
  }
  return { tint: 'mut', label: job.status || 'pending' }
}

// --- receipts and promotion artifacts --------------------------------------

/** One receipt, validated: { kind, ref, at } with `at` epoch ms or null. */
export function parseReceipt(input) {
  if (!isPlainObject(input)) return null
  const kind = boundedString(input.kind, BUILD_LIMITS.text)
  if (!kind || !RECEIPT_KIND_SET.has(kind)) return null
  const ref = boundedString(input.ref, BUILD_LIMITS.ref)
  if (!ref) return null
  const at = input.at == null ? null : toEpochMs(input.at)
  if (input.at != null && at === null) return null
  return Object.freeze({ kind, ref, at })
}

/** The well-formed receipts of a source row, malformed ones dropped, bounded. */
function receiptsOf(list) {
  if (!Array.isArray(list)) return []
  const out = []
  for (const item of list) {
    const r = parseReceipt(item)
    if (r) out.push(r)
    if (out.length >= BUILD_LIMITS.receipts) break
  }
  return out
}

/**
 * The promotion receipt a source row carries, or null. Recognised artifacts:
 *   { prewarm_relay: { schema: 'leaf.staging-prewarm-relay.v1', relay_run_id, dispatched } }
 *     promoted when the schema matches AND at least one target was dispatched
 *   { app_store_connect_result: { status, build_id } }
 *     promoted when status is a success status AND a build id exists
 *   { promotion_stage: { status, ref } }
 *     promoted when the janitor stage reports a promoted status
 * Anything else is NOT a promotion, including the mere presence of the key.
 */
export function promotionReceipt(input) {
  if (!isPlainObject(input)) return null
  const relay = input.prewarm_relay
  if (isPlainObject(relay) && relay.schema === PROMOTION_ARTIFACTS.prewarmRelaySchema) {
    const dispatched = Array.isArray(relay.dispatched) ? relay.dispatched.length : 0
    const runId = boundedString(String(relay.relay_run_id ?? ''), BUILD_LIMITS.ref)
    if (dispatched > 0 && runId) {
      return Object.freeze({ kind: 'promotion', ref: `${PROMOTION_ARTIFACTS.prewarmRelaySchema}#${runId}`, at: toEpochMs(relay.at) })
    }
    return null
  }
  const asc = input.app_store_connect_result
  if (isPlainObject(asc)) {
    const status = typeof asc.status === 'string' ? asc.status.toLowerCase() : ''
    const buildId = boundedString(String(asc.build_id ?? ''), BUILD_LIMITS.ref)
    if (PROMOTION_ARTIFACTS.appStoreConnectStatuses.includes(status) && buildId) {
      return Object.freeze({ kind: 'promotion', ref: `app_store_connect#${buildId}`, at: toEpochMs(asc.at) })
    }
    return null
  }
  const stage = input.promotion_stage
  if (isPlainObject(stage)) {
    const status = typeof stage.status === 'string' ? stage.status.toLowerCase() : ''
    const ref = boundedString(stage.ref, BUILD_LIMITS.ref)
    if (PROMOTION_ARTIFACTS.janitorPromotedStatuses.includes(status) && ref) {
      return Object.freeze({ kind: 'promotion', ref, at: toEpochMs(stage.at) })
    }
    return null
  }
  return null
}

function withPromotion(receipts, input) {
  const promo = promotionReceipt(input)
  if (!promo) return receipts
  if (receipts.some((r) => r.kind === 'promotion' && r.ref === promo.ref)) return receipts
  return receipts.length >= BUILD_LIMITS.receipts ? receipts : [...receipts, promo]
}

const hasPromotion = (receipts) => receipts.some((r) => r.kind === 'promotion')
const hasVerification = (receipts) => receipts.some((r) => r.kind === 'verification' || r.kind === 'gate-proof')
/** Any receipt kind that counts as terminal evidence: a broker job's own
 *  terminal receipt, or a verification/gate-proof receipt on any lane. The
 *  wire validator requires one of these before it accepts `verified: true`. */
const hasTerminalEvidence = (receipts) => receipts.some((r) => r.kind === 'terminal' || r.kind === 'verification' || r.kind === 'gate-proof')

// --- the record ------------------------------------------------------------

function freezeRecord(fields) {
  return Object.freeze({
    ...fields,
    receipts: Object.freeze(fields.receipts.map((r) => Object.freeze({ ...r }))),
    terminal: Object.freeze({ verified: !!fields.terminal.verified, promoted: !!fields.terminal.promoted }),
    actions: Object.freeze([...fields.actions]),
    status: Object.freeze({ ...fields.status }),
  })
}

/**
 * Validate one record crossing a boundary (GET /api/builds, a host prop).
 * Returns { ok: true, record } with a frozen, normalized record, or
 * { ok: false, reason } naming the FIRST defect. Never throws. Never repairs.
 */
export function parseBuildRecord(input) {
  const fail = (reason) => ({ ok: false, reason })
  if (!isPlainObject(input)) return fail('record: not an object')
  const id = boundedString(input.id, BUILD_LIMITS.id)
  if (!id) return fail('id: missing or over bound')
  if (!LANE_SET.has(input.lane)) return fail('lane: not one of fold | broker | fleet')
  if (!STATE_SET.has(input.state)) return fail('state: not one of queued | running | verifying | done | failed')
  const title = boundedString(input.title, BUILD_LIMITS.text)
  if (!title) return fail('title: missing or over bound')
  let requestedBy = null
  if (input.requested_by != null) {
    requestedBy = boundedString(input.requested_by, BUILD_LIMITS.text)
    if (!requestedBy) return fail('requested_by: not a bounded string')
  }
  let started = null
  if (input.started != null) {
    started = toEpochMs(input.started)
    if (started === null) return fail('started: not a timestamp')
  }
  let elapsed = null
  if (input.elapsed_ms != null) {
    elapsed = nonNegativeInt(input.elapsed_ms)
    if (elapsed === null) return fail('elapsed_ms: not a non-negative integer')
  }
  let estimate = null
  if (input.estimate_ms != null) {
    estimate = nonNegativeInt(input.estimate_ms)
    if (estimate === null || estimate === 0) return fail('estimate_ms: not a positive integer')
  }
  let cost = null
  if (input.cost_usd != null) {
    cost = nonNegative(input.cost_usd)
    if (cost === null) return fail('cost_usd: not a non-negative number')
  }
  if (!Array.isArray(input.receipts)) return fail('receipts: not an array')
  if (input.receipts.length > BUILD_LIMITS.receipts) return fail(`receipts: more than ${BUILD_LIMITS.receipts}`)
  const receipts = []
  for (let i = 0; i < input.receipts.length; i += 1) {
    const r = parseReceipt(input.receipts[i])
    if (!r) return fail(`receipts[${i}]: malformed (kind must be one of ${RECEIPT_KINDS.join(' | ')}, ref a bounded string, at a timestamp or null)`)
    receipts.push(r)
  }
  if (!isPlainObject(input.terminal)) return fail('terminal: not an object')
  if (typeof input.terminal.verified !== 'boolean') return fail('terminal.verified: not a boolean')
  if (typeof input.terminal.promoted !== 'boolean') return fail('terminal.promoted: not a boolean')
  // The cross-field rules the wire may not violate: a promotion is a
  // receipt, so `promoted` without a promotion receipt is an inference; a
  // verified build is verified BY a terminal, verification or gate-proof
  // receipt, so `verified` without one of those is a status word wearing a
  // verdict.
  if (input.terminal.promoted && !hasPromotion(receipts)) return fail('terminal.promoted: true without a promotion receipt')
  if (input.terminal.verified && !hasTerminalEvidence(receipts)) return fail('terminal.verified: true without a terminal, verification or gate-proof receipt')
  if (!Array.isArray(input.actions)) return fail('actions: not an array')
  if (input.actions.length > BUILD_ACTIONS.length) return fail('actions: more than the declared verbs')
  const actions = []
  for (const a of input.actions) {
    if (!ACTION_SET.has(a)) return fail('actions: not one of cancel | retry | promote')
    if (actions.includes(a)) return fail('actions: duplicate verb')
    actions.push(a)
  }
  if (!isPlainObject(input.status)) return fail('status: not an object')
  const word = boundedString(input.status.word, BUILD_LIMITS.text)
  if (!word) return fail('status.word: missing or over bound')
  if (!TINT_SET.has(input.status.tint)) return fail('status.tint: not one of ok | warn | err | mut')
  let detail = null
  if (input.status.detail != null) {
    detail = boundedString(input.status.detail, BUILD_LIMITS.text)
    if (!detail) return fail('status.detail: not a bounded string')
  }
  return {
    ok: true,
    record: freezeRecord({
      id,
      lane: input.lane,
      state: input.state,
      title,
      requested_by: requestedBy,
      started,
      elapsed_ms: elapsed,
      estimate_ms: estimate,
      cost_usd: cost,
      receipts,
      terminal: { verified: input.terminal.verified, promoted: input.terminal.promoted },
      actions,
      status: { word, tint: input.status.tint, detail },
    }),
  }
}

/** Throwing form of parseBuildRecord, for callers that own their input. */
export function validateBuildRecord(input) {
  const parsed = parseBuildRecord(input)
  if (!parsed.ok) throw new TypeError(`build record: ${parsed.reason}`)
  return parsed.record
}

/**
 * Parse a list off the wire: well-formed records kept in order, malformed
 * ones DROPPED and counted with their reasons, the list capped at the
 * records bound. Never throws.
 */
export function parseBuildRecords(list) {
  const records = []
  const dropped = []
  if (!Array.isArray(list)) return { records, dropped: [{ index: -1, reason: 'builds: not an array' }] }
  for (let i = 0; i < list.length && records.length < BUILD_LIMITS.records; i += 1) {
    const parsed = parseBuildRecord(list[i])
    if (parsed.ok) records.push(parsed.record)
    else dropped.push({ index: i, reason: parsed.reason })
  }
  return { records, dropped }
}

// --- lane mappers ----------------------------------------------------------

/**
 * BROKER: a job record as GET /api/jobs / useJobController hand it over:
 *   { job_id, tool, status, progress, elapsed_ms, degraded_mode, error,
 *     cost.usd_est, entitlement_required, created_at, receipts?, ... }
 * The status word, tint and detail are JobRail's, verbatim. `sessionId` names
 * the id for a current-session row that has no job id yet (mock runs).
 * Returns null when the row has no usable id or tool.
 */
export function fromBrokerJob(job, { sessionId = 'this-session' } = {}) {
  if (!isPlainObject(job)) return null
  const id = boundedString(job.job_id == null ? sessionId : String(job.job_id), BUILD_LIMITS.id)
  const title = clip(typeof job.tool === 'string' ? job.tool : null)
  if (!id || !title) return null
  const tag = brokerStateTag(job)
  let state
  if (job.status === 'running') state = 'running'
  else if (job.status === 'submitted' || job.status === 'queued') state = 'queued'
  else if (job.status === 'complete') state = 'done'
  else if (job.status === 'failed') state = 'failed'
  else return null
  const cost = state === 'done' ? brokerCostUsd(job) : null
  let detail = null
  if (state === 'failed') {
    const e = job.error
    detail = (e && (e.message || e.error_code)) || null
  } else if (state === 'running' && job.progress && job.progress !== 'running') {
    detail = job.progress
  } else if (state === 'done' && job.degraded_mode) {
    detail = 'local fallback'
  } else if (cost !== null) {
    detail = formatCostUsd(cost)
  }
  const receipts = withPromotion(receiptsOf(job.receipts), job)
  const actions = state === 'queued' || state === 'running' ? ['cancel'] : state === 'failed' ? ['retry'] : []
  return freezeRecord({
    id,
    lane: 'broker',
    state,
    title,
    requested_by: clip(typeof job.requested_by === 'string' ? job.requested_by : null),
    started: toEpochMs(job.created_at),
    elapsed_ms: nonNegativeInt(job.elapsed_ms),
    estimate_ms: null,
    cost_usd: cost,
    receipts,
    // A complete job is verified ONLY when its own terminal receipt (or a
    // verification/gate-proof receipt) is present. `state === 'done'` alone
    // is the status word the route already knows can outrun the receipt (a
    // missing, oversized or digest-mismatched file reads as absent), so it
    // is never enough on its own — including for a degraded_mode completion.
    terminal: { verified: state === 'done' && hasTerminalEvidence(receipts), promoted: hasPromotion(receipts) },
    actions,
    status: { word: tag.label, tint: tag.tint, detail: clip(typeof detail === 'string' ? detail : null) },
  })
}

/**
 * FOLD: a multi-round run's durable state file plus what sits beside it.
 *   state:     { run_id, rounds, spent_usd, mission_complete,
 *                mission_complete_vacuous, unverified_complete, escalated,
 *                round_in_progress, milestones: { id: { status, attempts,
 *                verified_at, last_error, artifacts } } }
 *   meta:      { run_id?, started_at?, state_mtime?, requested_by?,
 *                receipts?, prewarm_relay? | app_store_connect_result? |
 *                promotion_stage? }
 * Verified means a milestone carries verified_at under a real oracle
 * (mission_complete without mission_complete_vacuous). An exhausted or
 * vacuous run is NOT verified, whatever it claims.
 */
export function fromFoldState(state, meta = {}) {
  if (!isPlainObject(state) || !isPlainObject(meta)) return null
  const runId = boundedString(state.run_id ?? meta.run_id, BUILD_LIMITS.id)
  if (!runId) return null
  const milestones = isPlainObject(state.milestones) ? Object.entries(state.milestones) : []
  const total = milestones.length
  let done = 0
  let verifiedAt = null
  let lastError = null
  for (const [, m] of milestones) {
    if (!isPlainObject(m)) continue
    if (m.status === 'done') done += 1
    if (typeof m.verified_at === 'string' && m.verified_at.length && toEpochMs(m.verified_at) !== null) {
      verifiedAt = verifiedAt === null ? m.verified_at : (m.verified_at > verifiedAt ? m.verified_at : verifiedAt)
    }
    if (typeof m.last_error === 'string' && m.last_error.length) lastError = m.last_error
  }
  const rounds = nonNegativeInt(state.rounds) ?? 0
  const escalated = typeof state.escalated === 'string' && state.escalated.length > 0
  const vacuous = state.mission_complete_vacuous === true
  const complete = state.mission_complete === true && !vacuous
  const unverified = state.unverified_complete === true
  const inRound = isPlainObject(state.round_in_progress)
  const progress = total ? `${done}/${total} milestones` : null

  let stateWord
  let status
  let verified = false
  let actions
  if (escalated) {
    stateWord = 'failed'
    status = { word: 'escalated', tint: 'err', detail: clip(state.escalated) }
    actions = ['retry']
  } else if (complete) {
    verified = verifiedAt !== null
    stateWord = verified ? 'done' : 'verifying'
    status = verified
      ? { word: 'verified', tint: 'ok', detail: progress }
      : { word: 'unverified', tint: 'warn', detail: 'complete without a milestone verification' }
    actions = verified ? [] : ['retry']
  } else if (vacuous) {
    stateWord = 'verifying'
    status = { word: 'unverified', tint: 'warn', detail: 'completed with no oracle' }
    actions = ['retry']
  } else if (unverified) {
    stateWord = 'failed'
    status = { word: 'unverified', tint: 'warn', detail: lastError ? clip(lastError) : 'stopped before the oracle passed' }
    actions = ['retry']
  } else if (inRound || rounds > 0) {
    stateWord = 'running'
    status = { word: `round ${inRound && nonNegativeInt(state.round_in_progress.round) != null ? state.round_in_progress.round : rounds}`, tint: 'ok', detail: progress }
    actions = ['cancel']
  } else {
    stateWord = 'queued'
    status = { word: 'queued', tint: 'mut', detail: progress }
    actions = ['cancel']
  }
  const receipts = withPromotion(
    verified
      ? withVerification(receiptsOf(meta.receipts), runId, verifiedAt)
      : receiptsOf(meta.receipts),
    meta,
  )
  const promoted = hasPromotion(receipts)
  if (stateWord === 'done' && verified && !promoted) actions = ['promote']
  const started = toEpochMs(meta.started_at)
  const mtime = toEpochMs(meta.state_mtime)
  return freezeRecord({
    id: runId,
    lane: 'fold',
    state: stateWord,
    title: clip(typeof meta.title === 'string' ? meta.title : runId),
    requested_by: clip(typeof meta.requested_by === 'string' ? meta.requested_by : null),
    started,
    elapsed_ms: started !== null && mtime !== null && mtime >= started ? mtime - started : null,
    estimate_ms: null,
    cost_usd: nonNegative(state.spent_usd),
    receipts,
    terminal: { verified, promoted },
    actions,
    status: { word: status.word, tint: status.tint, detail: status.detail },
  })
}

function withVerification(receipts, runId, verifiedAt) {
  if (receipts.some((r) => r.kind === 'verification')) return receipts
  if (receipts.length >= BUILD_LIMITS.receipts) return receipts
  return [...receipts, Object.freeze({ kind: 'verification', ref: `${runId}#verified_at`, at: toEpochMs(verifiedAt) })]
}

// The collector's task_state vocabulary -> the card's coarse state and the
// collector's own word, kept. waiting_human / blocked / stalled are still open
// work, so they stay 'running' with an honest amber word.
const FLEET_STATES = Object.freeze({
  active: { state: 'running', word: 'active', tint: 'ok' },
  idle: { state: 'running', word: 'idle', tint: 'mut' },
  waiting_human: { state: 'running', word: 'waiting on a person', tint: 'warn' },
  blocked: { state: 'running', word: 'blocked', tint: 'warn' },
  stalled: { state: 'running', word: 'stalled', tint: 'warn' },
  queued: { state: 'queued', word: 'queued', tint: 'mut' },
  complete: { state: 'done', word: 'complete', tint: 'ok' },
  failed: { state: 'failed', word: 'failed', tint: 'err' },
  abandoned: { state: 'failed', word: 'abandoned', tint: 'err' },
})

/**
 * FLEET: one collector row, task_state joined to tasks:
 *   { task_id, title, owner, state, state_since, last_evidence_at, detail,
 *     terminal_state, created_at, requested_by?, receipts?, estimate_ms?,
 *     cost_usd?, prewarm_relay? | app_store_connect_result? | promotion_stage? }
 * `requested_by` is the gateway's own stamp when present (slice 11b); until
 * then it is `null`. `owner` names who is EXECUTING the task, not who asked
 * for it, so it is never substituted in as a requester.
 * A complete task is verified ONLY by a verification or gate-proof receipt:
 * the collector's 'complete' is transcript evidence, not a verdict.
 */
export function fromFleetTask(row) {
  if (!isPlainObject(row)) return null
  const id = boundedString(typeof row.task_id === 'string' ? row.task_id : null, BUILD_LIMITS.id)
  if (!id) return null
  const mapped = FLEET_STATES[typeof row.state === 'string' ? row.state : '']
  if (!mapped) return null
  const receipts = withPromotion(receiptsOf(row.receipts), row)
  const verified = mapped.state === 'done' && hasVerification(receipts)
  const promoted = hasPromotion(receipts)
  let actions
  if (mapped.state === 'queued' || mapped.state === 'running') actions = ['cancel']
  else if (mapped.state === 'failed') actions = ['retry']
  else actions = verified && !promoted ? ['promote'] : []
  const started = toEpochMs(row.created_at)
  const last = toEpochMs(row.last_evidence_at)
  const requestedBy = clip(typeof row.requested_by === 'string' ? row.requested_by : null)
  return freezeRecord({
    id,
    lane: 'fleet',
    state: mapped.state,
    title: clip(typeof row.title === 'string' && row.title.length ? row.title : id),
    requested_by: requestedBy,
    started,
    elapsed_ms: started !== null && last !== null && last >= started ? last - started : null,
    estimate_ms: (() => { const e = nonNegativeInt(row.estimate_ms); return e ? e : null })(),
    cost_usd: nonNegative(row.cost_usd),
    receipts,
    terminal: { verified, promoted },
    actions,
    status: {
      word: mapped.word,
      tint: mapped.tint,
      detail: clip(typeof row.detail === 'string' ? row.detail : null),
    },
  })
}

// --- host helpers ----------------------------------------------------------

/** Open work: queued, running or verifying. The toolbar badge's number. */
export function runningBuildCount(records) {
  if (!Array.isArray(records)) return 0
  let n = 0
  for (const r of records) if (r && OPEN_STATES.has(r.state)) n += 1
  return n
}

/** Whether a record is terminal (the row becomes a button in the rail). */
export function isTerminalBuild(record) {
  return !!record && (record.state === 'done' || record.state === 'failed')
}

/**
 * Whether an in-flight current-session job should add one to a running
 * count: it must be open work AND not already counted among `jobs` by its
 * own job_id. The ONE definition of this rule (JobRail's spine count and the
 * toolbar badge both call it) so the two can never again show two different
 * numbers for the same open work.
 */
export function currentJobCountsAsRunning(currentJob, jobs) {
  if (!currentJob || (currentJob.status !== 'running' && currentJob.status !== 'submitted')) return false
  if (!currentJob.job_id) return true
  const list = Array.isArray(jobs) ? jobs : []
  return !list.some((j) => j.job_id === currentJob.job_id)
}
