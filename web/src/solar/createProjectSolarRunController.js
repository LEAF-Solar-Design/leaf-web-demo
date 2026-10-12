import { createSolarDrawingBinding, sameSolarDrawingBinding } from './solarDrawingBinding.js'
import { PROJECT_SOLAR_RUN_REASONS, projectSolarRunFailure, readProjectSolarCompletion } from './solarProjectCompletion.js'

export const PROJECT_SOLAR_RUN_POLL_MS = 1000
export const PROJECT_SOLAR_RUN_BUDGET_MS = 120000

const UUID = new RegExp('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
const CODE = new RegExp('^[A-Za-z][A-Za-z0-9_]{0,127}$')
const TOOL = new RegExp('^[a-z0-9][a-z0-9-]{0,127}$')
const DIGEST = new RegExp('^sha256:[0-9a-f]{64}$')
const assert = (value) => { if (!value) throw new TypeError('Invalid run request') }
const plain = (value) => value !== null && typeof value === 'object' &&
  [Object.prototype, null].includes(Object.getPrototypeOf(value))
function field(value, key, required = true) {
  assert(plain(value))
  const descriptor = Object.getOwnPropertyDescriptor(value, key)
  if (!descriptor) { assert(!required); return undefined }
  assert(Object.hasOwn(descriptor, 'value'))
  return descriptor.value
}
const local = (reason, status = null) => ({ ok: false, status, reason, envelope: null })
const outcome = (state, jobId = null, reasonCode = null, message = null) =>
  Object.freeze({ state, jobId, outputVersionId: null, reasonCode, message })
const unknown = (jobId) => outcome('outcome_unknown', jobId, null, PROJECT_SOLAR_RUN_REASONS.outcome_unknown)
const unreadable = (jobId) => outcome('unreadable', jobId, null, PROJECT_SOLAR_RUN_REASONS.unreadable_output)
const deferred = () => {
  let resolve
  const promise = new Promise((done) => { resolve = done })
  return { promise, resolve }
}

// Only the transport projection is inspected. Unrelated envelope data stays private to R7.
function transport(value, submitting) {
  let definiteFailure = false
  try {
    const ok = field(value, 'ok')
    assert(typeof ok === 'boolean')
    definiteFailure = ok === false
    const status = field(value, 'status', ok)
    if (ok) {
      assert(status === (submitting ? 202 : 200))
      const data = field(value, 'data')
      if (submitting) {
        const jobId = field(data, 'job_id')
        assert(typeof jobId === 'string' && UUID.test(jobId) && field(data, 'status') === 'submitted')
        return { kind: 'accepted', jobId }
      }
      assert(plain(data))
      const wrapper = Object.getOwnPropertyDescriptor(data, 'job')
      let job = data
      if (wrapper) {
        assert(Object.hasOwn(wrapper, 'value') && plain(wrapper.value))
        job = wrapper.value
        for (const key of ['job_id', 'org_id', 'project_id', 'input_version_id', 'tool_name', 'execution_context', 'status', 'output_version_id']) {
          if (Object.getOwnPropertyDescriptor(data, key)) {
            const outer = field(data, key)
            const inner = field(job, key)
            if (key === 'execution_context') {
              assert(field(outer, 'schema') === field(inner, 'schema') &&
                field(outer, 'drawing_id') === field(inner, 'drawing_id'))
            } else assert(outer === inner)
          }
        }
      }
      return { kind: 'job', job }
    }
    const rawReason = field(value, 'reason')
    const reason = typeof rawReason === 'string' && CODE.test(rawReason) ? rawReason : 'HTTP_ERROR'
    const safeStatus = status === null || (Number.isInteger(status) && status >= 100 && status <= 599) ? status : null
    if (reason === 'SIP_R7_ABORTED') return { kind: 'aborted' }
    if (reason === 'SIP_R7_SCOPE_STALE') return { kind: 'stale' }
    if (reason === 'SIP_R7_TRANSPORT_FAILED' || reason === 'SIP_R7_TIMEOUT') return { kind: 'lost' }
    if (!submitting && reason === 'SIP_R7_RESPONSE_INVALID') return { kind: 'unreadable' }
    if (submitting && (reason === 'SIP_R7_REQUEST_INVALID' || (safeStatus >= 400 && safeStatus < 500))) {
      const failure = projectSolarRunFailure(reason)
      return { kind: 'refused', status: safeStatus, failure,
        reason: reason === 'SIP_R7_REQUEST_INVALID' ? reason : failure.reasonCode || 'HTTP_ERROR' }
    }
    return { kind: 'unknown' }
  } catch { return { kind: submitting || definiteFailure ? 'unknown' : 'unreadable' } }
}

export function createProjectSolarRunController({ projectClient, onOutcome = () => {} } = {}) {
  if (!projectClient || !['submitRun', 'readJob', 'getSnapshot', 'subscribe'].every((key) =>
    typeof projectClient[key] === 'function') || typeof onOutcome !== 'function') {
    throw new TypeError('A project run client and outcome observer are required')
  }
  let disposed = false
  let association = null
  const active = new Set()
  // One subscription for the controller's life. The epoch moves whenever the client's binding or
  // authority changes, so a guard published after settlement stays false once its scope changed.
  let scopeEpoch = 0
  let scopeSeen = null
  let unsubscribeScope = null
  function matches(run, binding, authorityKey) {
    return run.authorityKey === authorityKey && sameSolarDrawingBinding(run.binding, binding)
  }
  function scoped(run) {
    try {
      const snapshot = projectClient.getSnapshot()
      return !disposed && !run.stale && !run.callerSignal?.aborted && run.epoch === scopeEpoch &&
        run.isCurrent() === true && matches(run, field(snapshot, 'binding'), field(snapshot, 'authorityKey'))
    } catch { return false }
  }
  function readScope() {
    try {
      const snapshot = projectClient.getSnapshot()
      // A frozen copy, so a binding object changed in place still moves the epoch.
      let binding = null
      try { binding = createSolarDrawingBinding(field(snapshot, 'binding')) } catch { binding = null }
      return { binding, authorityKey: field(snapshot, 'authorityKey') }
    } catch { return null }
  }
  function observeScope() {
    const next = readScope()
    if (!next || !scopeSeen || next.authorityKey !== scopeSeen.authorityKey ||
      !sameSolarDrawingBinding(next.binding, scopeSeen.binding)) scopeEpoch += 1
    scopeSeen = next
    for (const run of active) if (!scoped(run)) interrupt(run, 'stale')
  }
  function watchScope() {
    if (unsubscribeScope) return
    let cleanup = null
    try { cleanup = projectClient.subscribe(observeScope) } catch { cleanup = null }
    if (typeof cleanup !== 'function') throw new TypeError('The project subscription must return cleanup')
    unsubscribeScope = cleanup
    scopeSeen = readScope()
  }
  function interrupt(run, state) {
    if (run.done || run.interruption) return
    run.interruption = state
    if (state === 'stale') {
      run.stale = true
      if (association === run) association = null
    }
    run.cancel.resolve(state)
    run.controller.abort()
  }
  function check(run) {
    if (run.interruption) return run.interruption
    if (disposed || run.callerSignal?.aborted) interrupt(run, 'aborted')
    else if (!scoped(run)) interrupt(run, 'stale')
    else if (Date.now() >= run.deadline) interrupt(run, 'outcome_unknown')
    return run.interruption
  }
  function finish(run, result) {
    if (run.done) return
    const stopped = check(run)
    if (stopped) result = stopped === 'outcome_unknown' ? unknown(run.jobId) : outcome(stopped, run.jobId)
    clearTimeout(run.deadlineTimer)
    clearInterval(run.currentTimer)
    clearTimeout(run.delayTimer)
    run.callerSignal?.removeEventListener('abort', run.abortListener)
    run.preparedIntent = null
    active.delete(run)
    const finalStopped = check(run)
    if (finalStopped) result = finalStopped === 'outcome_unknown' ? unknown(run.jobId) : outcome(finalStopped, run.jobId)
    run.done = true
    run.settlement.resolve(result)
    // Release every pending race so no raw envelope outlives the settlement.
    run.cancel.resolve('settled')
    if (!['aborted', 'stale'].includes(result.state) && scoped(run)) {
      try {
        onOutcome(result, Object.freeze({ binding: run.binding, authorityKey: run.authorityKey,
          tool: run.tool, signal: run.controller.signal, isCurrent: () => scoped(run) }))
      } catch { /* A notification is synchronous and is never retried. */ }
    }
  }
  async function wait(run, operation) {
    const stopped = check(run)
    if (stopped) throw stopped
    const resolved = Promise.resolve().then(() => {
      const before = check(run)
      if (before) throw before
      return operation()
    }).then(
      (value) => ({ value }), () => ({ lost: true }))
    const result = await Promise.race([resolved, run.cancel.promise.then((state) => ({ stopped: state }))])
    const after = check(run)
    if (after || result.stopped) throw after || result.stopped
    return result
  }
  async function request(run, submitting) {
    const result = await wait(run, () => {
      if (!submitting) return projectClient.readJob(run.jobId, { tool: run.tool, signal: run.controller.signal })
      // From here the request may reach the server, so no interruption can answer that nothing happened.
      run.sent = true
      return projectClient.submitRun(run.preparedIntent, { signal: run.controller.signal })
    })
    const parsed = result.lost ? { kind: 'lost' } : transport(result.value, submitting)
    const stopped = check(run)
    if (stopped) throw stopped
    if (parsed.kind === 'stale' || parsed.kind === 'aborted') {
      interrupt(run, parsed.kind)
      throw parsed.kind
    }
    return parsed
  }
  async function follow(run) {
    for (;;) {
      let recovering = false
      let read = await request(run, false)
      if (read.kind === 'lost' && !run.recovered) {
        run.recovered = true
        recovering = true
        read = await request(run, false)
        if (read.kind !== 'job') { finish(run, unknown(run.jobId)); return }
      }
      if (read.kind !== 'job') {
        finish(run, read.kind === 'unreadable' ? unreadable(run.jobId) : unknown(run.jobId))
        return
      }
      const result = readProjectSolarCompletion({ job: read.job, binding: run.binding, jobId: run.jobId, tool: run.tool })
      read = null
      if (result.state !== 'pending') {
        finish(run, recovering && result.state === 'unreadable' ? unknown(run.jobId) : result)
        return
      }
      await wait(run, () => new Promise((resolve) => {
        run.delayTimer = setTimeout(resolve, PROJECT_SOLAR_RUN_POLL_MS)
      }))
    }
  }
  async function execute(run) {
    try {
      let submitted = await request(run, true)
      if (submitted.kind === 'lost') {
        run.recovered = true
        submitted = await request(run, true)
        if (submitted.kind !== 'accepted') submitted = { kind: 'unknown' }
      }
      const stopped = check(run)
      if (stopped) throw stopped
      run.preparedIntent = null
      if (submitted.kind === 'accepted') {
        run.jobId = submitted.jobId
        run.submission.resolve({ ok: true, status: 202, data: { job_id: run.jobId, status: 'submitted' } })
        await follow(run)
      } else if (submitted.kind === 'refused') {
        run.submission.resolve(local(submitted.reason, submitted.status))
        finish(run, outcome('refused', null, submitted.failure.reasonCode,
          submitted.reason === 'SIP_R7_REQUEST_INVALID' ? PROJECT_SOLAR_RUN_REASONS.request_invalid : submitted.failure.message))
      } else {
        run.submission.resolve(local('SIP_R13B_OUTCOME_UNKNOWN'))
        finish(run, unknown(run.jobId))
      }
    } catch (state) {
      const interruption = ['stale', 'aborted', 'outcome_unknown'].includes(state) ? state : 'outcome_unknown'
      // A scope change or abort answers that nothing happened only when no submit request was ever made.
      run.submission.resolve(local(run.sent || interruption === 'outcome_unknown' ? 'SIP_R13B_OUTCOME_UNKNOWN' :
        interruption === 'stale' ? 'SIP_R7_SCOPE_STALE' : 'SIP_R7_ABORTED'))
      finish(run, interruption === 'outcome_unknown' ? unknown(run.jobId) : outcome(interruption, run.jobId))
    }
  }
  function onProjectSubmit(preparedIntent, options) {
    let binding, authorityKey, tool, signal, isCurrent
    try {
      binding = createSolarDrawingBinding(field(options, 'binding'))
      authorityKey = field(options, 'authorityKey')
      signal = field(options, 'signal', false)
      isCurrent = field(options, 'isCurrent')
      assert(typeof authorityKey === 'string' && authorityKey.length > 0 &&
        new TextEncoder().encode(authorityKey).byteLength <= 256 && typeof isCurrent === 'function')
      assert(signal === undefined || (signal && typeof signal.aborted === 'boolean' &&
        typeof signal.addEventListener === 'function' && typeof signal.removeEventListener === 'function'))
      assert(plain(preparedIntent) && Reflect.ownKeys(preparedIntent).length === 6)
      for (const key of ['binding', 'authorityKey', 'tool', 'params', 'catalogDigest', 'idempotencyKey']) field(preparedIntent, key)
      assert(sameSolarDrawingBinding(field(preparedIntent, 'binding'), binding) &&
        field(preparedIntent, 'authorityKey') === authorityKey)
      tool = field(preparedIntent, 'tool')
      const digest = field(preparedIntent, 'catalogDigest')
      const key = field(preparedIntent, 'idempotencyKey')
      assert(typeof tool === 'string' && TOOL.test(tool) && typeof digest === 'string' && DIGEST.test(digest) &&
        typeof key === 'string' && key.trim().length > 0 && new TextEncoder().encode(key).byteLength <= 256 &&
        plain(field(preparedIntent, 'params')))
    } catch { return Promise.resolve(local('SIP_R7_REQUEST_INVALID')) }
    if (disposed || signal?.aborted) return Promise.resolve(local('SIP_R7_ABORTED'))
    watchScope()
    const probe = { binding, authorityKey, isCurrent, controller: new AbortController(), stale: false,
      epoch: scopeEpoch }
    if (!scoped(probe)) return Promise.resolve(local('SIP_R7_SCOPE_STALE'))
    for (const run of active) {
      if (!run.interruption && matches(run, binding, authorityKey)) return Promise.resolve(local('SIP_R13B_RUN_IN_PROGRESS'))
    }
    const run = { ...probe, tool, preparedIntent, callerSignal: signal, jobId: null,
      deadline: Date.now() + PROJECT_SOLAR_RUN_BUDGET_MS, recovered: false, sent: false, done: false,
      interruption: null, cancel: deferred(), submission: deferred(), settlement: deferred() }
    active.add(run)
    association = run
    run.abortListener = () => interrupt(run, 'aborted')
    signal?.addEventListener('abort', run.abortListener, { once: true })
    run.deadlineTimer = setTimeout(() => interrupt(run, 'outcome_unknown'), PROJECT_SOLAR_RUN_BUDGET_MS)
    run.currentTimer = setInterval(() => check(run), PROJECT_SOLAR_RUN_POLL_MS)
    void execute(run)
    return run.submission.promise
  }
  return Object.freeze({
    onProjectSubmit,
    whenSettled(binding, authorityKey) {
      if (association && !scoped(association)) {
        if (!association.done) interrupt(association, 'stale')
        association = null
      }
      return association && matches(association, binding, authorityKey) ? association.settlement.promise : Promise.resolve(null)
    },
    dispose() {
      if (disposed) return
      disposed = true
      association = null
      for (const run of active) interrupt(run, 'aborted')
      try { unsubscribeScope?.() } catch { /* Disposal cannot fail. */ }
      unsubscribeScope = null
    },
  })
}
