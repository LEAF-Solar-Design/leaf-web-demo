import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createProjectSolarRunController, PROJECT_SOLAR_RUN_BUDGET_MS } from './createProjectSolarRunController.js'
import { createSolarDrawingBinding, sameSolarDrawingBinding } from './solarDrawingBinding.js'
import { projectSolarAppReason } from './solarProjectAppModel.js'
import { PROJECT_SOLAR_RUN_REASONS } from './solarProjectCompletion.js'
import { projectSolarDrawingView } from './solarProjectDrawingModel.js'
import { recordProjectSolarFlowRun } from './solarFlowModel.js'
import { humanizeError } from '../errorHumanize.js'

const refusal = reason => ({ ok: false, status: null, reason, envelope: null })
const current = predicate => { try { return predicate?.() === true } catch { return false } }
const deferred = () => {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}
const empty = () => ({ context: null, openTool: null, toolOwner: null, projectRunMemory: null,
  projectPending: null, projectOutcome: null, busy: false, notice: null,
  publishedVersionId: null, visibleLayers: {} })
function identity(inputs) {
  const ws = inputs.workspaceController.getSnapshot()
  const session = inputs.sessionController.getSnapshot()
  return [ws.orgId, ws.openProjectId, ws.canonicalVersionId, session.status, session.generation]
}
const equal = (a, b) => a.length === b.length && a.every((value, index) => value === b[index])
const sameOwner = (a, b) => a?.projectClient === b?.projectClient &&
  a?.scope?.authorityKey === b?.scope?.authorityKey && sameSolarDrawingBinding(a?.scope?.binding, b?.scope?.binding)
function catalogRows(inputs) {
  const a = inputs.projectSolar, receipt = inputs.projectCatalog
  if (!receipt || receipt.client !== a.projectClient || receipt.authorityKey !== a.scope.authorityKey ||
      !sameSolarDrawingBinding(receipt.binding, a.scope.binding) || !current(receipt.isCurrent)) return []
  try { return receipt.families.flatMap(family => family.capabilities) } catch { return [] }
}

// App supplies every owner. The bridge owns observation; this hook owns selection/readback.
export function useAppProjectSolarRuntime(props) {
  // useWorkspaceController returns a fresh snapshot facade each render. Its
  // observation and selection methods identify the underlying controller.
  const workspaceController = useMemo(() => props.workspaceController, [
    props.workspaceController.getSnapshot, props.workspaceController.subscribe,
    props.workspaceController.rehydrate, props.workspaceController.selectCanonicalVersion,
  ])
  const inputs = { ...props, workspaceController }
  const latest = useRef(inputs)
  const machine = useRef({ alive: false, epoch: 0, seen: null, owner: null, run: null,
    transition: null, request: null })
  const [state, setState] = useState(empty)
  const [revision, refresh] = useState(0)
  const m = machine.current
  useLayoutEffect(() => { latest.current = inputs })

  function publish(guard, patch) {
    if (guard()) setState(previous => guard() ? { ...previous,
      ...(typeof patch === 'function' ? patch(previous) : patch) } : previous)
  }
  function retire() {
    const t = m.transition
    if (t) { t.retired = true; settle(t, false); m.transition = null }
    m.request?.abort()
    m.owner?.abort.abort()
    m.run = null
  }
  function observe() {
    const next = identity(latest.current)
    if (m.seen && !equal(next, m.seen)) {
      const t = m.transition
      const intentional = t?.selecting && !t.selected &&
        equal(next, [t.identity[0], t.identity[1], t.output.input_version_id, t.identity[3], t.identity[4]])
      m.epoch += 1
      m.request?.abort()
      if (intentional) { t.selected = true; t.epoch = m.epoch; t.identity = next }
      else retire()
      m.seen = next
      const epoch = m.epoch
      publish(() => m.alive && m.epoch === epoch, previous => intentional
        ? { context: null, openTool: null, visibleLayers: {} }
        : { ...empty(), projectRunMemory: previous.projectRunMemory })
      refresh(value => m.alive && m.epoch === epoch ? value + 1 : value)
    } else m.seen = next
  }
  useLayoutEffect(() => {
    m.alive = true
    m.seen = identity(inputs)
    const offWorkspace = inputs.workspaceController.subscribe(observe)
    const offSession = inputs.sessionController.subscribe(observe)
    return () => { m.alive = false; retire(); offWorkspace(); offSession() }
  }, [inputs.workspaceController, inputs.sessionController])

  function ownerCurrent(owner) {
    observe()
    const live = latest.current, a = live.projectSolar, b = owner?.a.scope.binding
    return m.alive && live.projectMode === true && !!owner && owner === m.owner && !owner.abort.signal.aborted &&
      owner.epoch === m.epoch && a?.ready === true && sameOwner(owner.a, a) && current(a.isCurrent) &&
      equal(m.seen, [b.organization_id, b.project_id, b.input_version_id, 'active', owner.sessionGeneration])
  }
  function transitionCurrent(t) {
    observe()
    const live = latest.current, a = live.projectSolar
    // Until the output is selected the bridge notification must stay current; its scope epoch only
    // moves forward, so a round trip through another authority or binding is never forgotten.
    return m.alive && live.projectMode === true && m.transition === t && !t.retired &&
      (t.selected || current(t.meta?.isCurrent)) &&
      t.epoch === m.epoch && equal(t.identity, m.seen) &&
      live.workspaceController === t.workspace && live.sessionController === t.session &&
      // A can briefly be unready between selection and installation of the output scope.
      (!a.ready || (a.projectClient === t.client && a.scope.authorityKey === t.authorityKey &&
        (sameSolarDrawingBinding(a.scope.binding, t.binding) ||
          (t.selected && sameSolarDrawingBinding(a.scope.binding, t.output)))))
  }
  function unreadable(t) {
    if (t.phase === 'done' || !transitionCurrent(t)) return false
    t.phase = t.selected ? 'context' : 'selection'
    t.pending = false
    settle(t, false)
    publish(() => transitionCurrent(t), { busy: false, notice: projectSolarAppReason('output_unreadable') })
    return false
  }
  function settle(t, value) { clearTimeout(t.timer); t.wait.resolve(value) }
  // A completion whose notification left scope before selection is not unreadable: it is retired,
  // and the runtime returns to idle, because no identity change will come along to retire it.
  function scopeLost(t) {
    return m.transition === t && !t.retired && !t.selected && !current(t.meta?.isCurrent)
  }
  function lose(t) {
    t.retired = true
    settle(t, false)
    m.transition = null
    m.request?.abort()
    m.run = null
    const epoch = m.epoch
    publish(() => m.alive && m.epoch === epoch, previous => ({ ...empty(), projectRunMemory: previous.projectRunMemory }))
    refresh(value => m.alive && m.epoch === epoch ? value + 1 : value)
  }
  function boundReadback(t) {
    t.timer = setTimeout(() => {
      if (transitionCurrent(t)) { m.request?.abort(); unreadable(t) }
      else if (scopeLost(t)) lose(t)
      else settle(t, false)
    }, PROJECT_SOLAR_RUN_BUDGET_MS)
  }
  async function selectOutput(t) {
    try {
      const workspace = await Promise.race([t.workspace.rehydrate(), t.wait.promise.then(() => null)])
      if (!transitionCurrent(t)) { if (scopeLost(t)) lose(t); return false }
      const rows = Array.isArray(workspace?.drawing_versions)
        ? workspace.drawing_versions.filter(row => row?.version_id === t.output.input_version_id) : []
      const row = rows.length === 1 ? rows[0] : null
      if (!row || (workspace.project?.project_id || workspace.project?.id) !== t.binding.project_id ||
          row.org_id !== t.binding.organization_id || row.project_id !== t.binding.project_id ||
          row.drawing_id !== t.binding.drawing_id || t.workspace.getSnapshot().workspace !== workspace) return unreadable(t)
      t.selecting = true
      t.workspace.selectCanonicalVersion(t.output.input_version_id)
      t.selecting = false
      if (!transitionCurrent(t) || !t.selected) { settle(t, false); return false }
      t.phase = 'context'
      return t.wait.promise
    } catch {
      if (scopeLost(t)) { lose(t); return false }
      return unreadable(t)
    }
  }

  function onOutcome(owner, outcome, meta) {
    const run = m.run
    if (!run || run.owner !== owner || run.terminal || !run.current() || !current(meta.isCurrent) ||
        meta.signal.aborted || !sameSolarDrawingBinding(meta.binding, run.binding) ||
        meta.authorityKey !== run.authorityKey || meta.tool !== run.tool) return
    run.terminal = true
    run.outcome = outcome
    run.meta = meta
    // An accepted job whose outcome is unknown may still commit: keep its lock (see staleRun).
    if (outcome.state === 'outcome_unknown') run.uncertain = true
    const terminal = ['succeeded', 'failed', 'proposal'].includes(outcome.state)
    // A queued terminal update may commit after App has already selected the output.
    const notificationCurrent = () => run.current() ||
      (m.transition?.outcome === outcome && transitionCurrent(m.transition))
    publish(notificationCurrent, previous => ({ projectPending: null, projectOutcome: { outcome, meta },
      projectRunMemory: terminal ? recordProjectSolarFlowRun(previous.projectRunMemory, meta, {
        terminal: true, ok: outcome.state === 'succeeded' || outcome.state === 'proposal',
        ...(outcome.reasonCode ? { reason_code: outcome.reasonCode } : {}),
      }) : previous.projectRunMemory,
      busy: outcome.state === 'succeeded', notice: outcome.state === 'outcome_unknown'
        ? PROJECT_SOLAR_RUN_REASONS.outcome_unknown : outcome.state === 'unreadable'
          ? PROJECT_SOLAR_RUN_REASONS.unreadable_output : outcome.message
            ? humanizeError(outcome.message) : outcome.reasonCode
              ? humanizeError('This solar request could not be completed. Check the inputs before trying again.') : null }))
    if (outcome.state === 'succeeded') {
      // Dispatch only; App's explicit completion channel calls complete below.
      const completionFailed = () => { const t = m.transition; if (t && t.outcome === outcome) unreadable(t) }
      try {
        Promise.resolve(latest.current.completedVersionRef.current?.(null, outcome, {
          projectSolarCompletion: meta,
        })).catch(completionFailed)
      } catch { completionFailed() }
    } else if (['failed', 'refused', 'proposal'].includes(outcome.state)) m.run = null
  }
  const a = inputs.projectSolar
  const bindingRef = useRef(null)
  if (!sameSolarDrawingBinding(bindingRef.current, a?.scope?.binding)) {
    try { bindingRef.current = createSolarDrawingBinding(a?.scope?.binding) } catch { bindingRef.current = null }
  }
  const binding = bindingRef.current
  const selectionCurrent = (previous) => inputs.projectMode === true &&
    previous.toolOwner?.authorityKey === a?.scope?.authorityKey &&
    sameSolarDrawingBinding(previous.toolOwner?.binding, a?.scope?.binding)
  const resetOwnerState = (previous) => ({ ...empty(), ...(selectionCurrent(previous)
    ? { openTool: previous.openTool, toolOwner: previous.toolOwner } : {}) })
  useEffect(() => {
    if (!inputs.projectMode || !a?.ready) {
      if (!m.transition) publish(() => m.alive && latest.current === inputs, resetOwnerState)
      return undefined
    }
    observe()
    const owner = { a, epoch: m.epoch, sessionGeneration: m.seen[4], abort: new AbortController() }
    owner.controller = createProjectSolarRunController({ projectClient: a.projectClient,
      onOutcome: (outcome, meta) => onOutcome(owner, outcome, meta) })
    m.owner = owner
    if (m.transition && !transitionCurrent(m.transition)) retire()
    if (!m.transition) publish(() => m.alive && m.owner === owner, resetOwnerState)
    refresh(value => m.alive && m.owner === owner ? value + 1 : value)
    return () => {
      owner.abort.abort()
      owner.controller.dispose()
      if (m.owner === owner) m.owner = null
      const t = m.transition
      if (t && (!t.selected || !latest.current.projectMode || !transitionCurrent(t))) retire()
      if (!m.transition && m.run?.owner === owner) m.run = null
    }
  }, [inputs.projectMode, a?.ready, a?.projectClient, binding, a?.scope?.authorityKey,
    inputs.workspaceController, inputs.sessionController, m.epoch])

  useEffect(() => {
    if (!inputs.projectMode || !a?.ready || !m.owner) return undefined
    const owner = m.owner
    // A capture rotation alone keeps the owner, so it never unlocks a pending or uncertain submission; only
    // a new owner (client, authority, binding, mode), a new identity or unmount releases it.
    const staleRun = m.run && !((m.run.submitting || m.run.uncertain || (m.run.accepted && !m.run.terminal)) && m.run.owner === m.owner) && !m.run.current() &&
      (!m.transition || m.transition.phase === 'done')
    if (staleRun) m.run = null
    // The old capture can no longer follow or complete an accepted job; retain its lock as unknown.
    if (m.run?.owner === owner && m.run.accepted && !m.run.terminal && !m.run.current()) m.run.uncertain = true
    // A render can see the previous capture before latest's layout effect runs.
    // Re-expose an already-read context after committing a valid replacement capture.
    const restoredContext = contextView === null && current(state.context?.current)
    if (staleRun || m.run?.uncertain || restoredContext) publish(() => ownerCurrent(owner), {
      ...(staleRun
      ? { projectPending: null, projectOutcome: null, busy: false, notice: null } : {}),
      ...(m.run?.uncertain ? { openTool: null, toolOwner: null, projectPending: null, busy: false,
        notice: PROJECT_SOLAR_RUN_REASONS.outcome_unknown } : {}) })
    return undefined
  }, [inputs.projectMode, a?.ready, a?.projectClient, a?.isCurrent, binding, a?.scope?.authorityKey, revision])

  useEffect(() => {
    if (!inputs.projectMode || !a?.ready || !m.owner) return undefined
    const owner = m.owner, epoch = m.epoch, controller = new AbortController()
    m.request?.abort()
    m.request = controller
    // Capture notifications can rotate while the same canonical read is in flight.
    // Check the live capture without making its function identity own the request.
    const valid = () => !controller.signal.aborted && epoch === m.epoch &&
      ownerCurrent(owner) && current(latest.current.projectSolar?.isCurrent)
    publish(valid, { context: null })
    async function read() {
      let response
      try { response = await a.projectClient.readContext({ signal: controller.signal }) } catch { response = null }
      if (!valid()) return
      const view = projectSolarDrawingView({ solarBinding: a.scope.binding,
        contextView: response?.ok === true ? response.data : null, current: true, scene: null })
      if (!valid()) return
      const t = m.transition
      if (response?.ok !== true || view.mode !== 'ready') {
        if (t && t.phase !== 'done' && transitionCurrent(t)) unreadable(t)
        // An uncertain run keeps its lock, so a failed read must not hide why the next submit is refused.
        else publish(valid, { notice: m.run?.uncertain ? PROJECT_SOLAR_RUN_REASONS.outcome_unknown
          : projectSolarAppReason('scope_pending') })
        return
      }
      publish(valid, { context: { data: response.data, current: valid },
        notice: m.run?.uncertain ? PROJECT_SOLAR_RUN_REASONS.outcome_unknown : null })
      if (t?.selected && t.phase !== 'done' && transitionCurrent(t) && sameSolarDrawingBinding(t.output, a.scope.binding)) {
        t.phase = 'done'
        t.pending = false
        publish(() => valid() && transitionCurrent(t), { busy: false })
        settle(t, true)
        m.run = null
      }
    }
    if (valid()) void read()
    return () => controller.abort()
  }, [inputs.projectMode, a?.ready, a?.projectClient, binding, a?.scope?.authorityKey, revision])

  // Callback entry belongs to this rendered owner, but reads live mode and gates.
  const renderEpoch = m.epoch
  const entryCurrent = () => latest.current.projectMode === true && inputs.projectMode === true &&
    sameOwner(a, latest.current.projectSolar) && renderEpoch === m.epoch
  const contextCurrent = () => entryCurrent() && current(state.context?.current)
  async function onProjectSubmit(prepared, options) {
    const owner = m.owner, live = latest.current
    if (!entryCurrent() || !live.hostEnabled || !ownerCurrent(owner)) return refusal('SIP_R7_SCOPE_STALE')
    if (m.run || live.externalBusy || (m.transition && m.transition.phase !== 'done')) return refusal('SIP_R13B_RUN_IN_PROGRESS')
    if (live.writeBlockedReason || !contextCurrent()) {
      publish(() => ownerCurrent(owner), { notice: projectSolarAppReason(live.writeBlockedReason || 'scope_pending') })
      return refusal('SIP_R7_REQUEST_INVALID')
    }
    const rows = catalogRows(live)
    if (!current(options?.isCurrent) || options?.signal?.aborted ||
        !sameSolarDrawingBinding(options?.binding, binding) || options?.authorityKey !== a.scope.authorityKey ||
        !sameSolarDrawingBinding(prepared?.binding, binding) || prepared?.authorityKey !== a.scope.authorityKey ||
        !rows.some(row => row?.name === prepared?.tool && row.catalog_digest === prepared?.catalogDigest)) return refusal('SIP_R7_SCOPE_STALE')
    const run = { owner, binding, authorityKey: a.scope.authorityKey, tool: prepared.tool, terminal: false, submitting: true,
      current: () => ownerCurrent(owner) && current(a.isCurrent) && current(options.isCurrent) && !options.signal?.aborted }
    m.run = run
    publish(run.current, { busy: true, notice: null, projectOutcome: null,
      projectPending: { binding, authorityKey: run.authorityKey, tool: run.tool } })
    const outcome = await owner.controller.onProjectSubmit(prepared, { ...options,
      signal: owner.abort.signal, isCurrent: run.current })
    run.submitting = false
    const accepted = outcome.ok === true && outcome.status === 202
    run.accepted = accepted
    if (accepted) {
      try {
        const settlement = owner.controller.whenSettled?.(binding, run.authorityKey)
        if (settlement != null) Promise.resolve(settlement).then(result => {
          const observing = () => m.alive && m.run === run && !run.terminal
          if (result == null || !observing()) return
          run.uncertain = true
          publish(observing, { projectPending: null, busy: false,
            notice: PROJECT_SOLAR_RUN_REASONS.outcome_unknown })
        }).catch(() => {})
      } catch { /* A bridge without a settlement observer keeps its existing behavior. */ }
    }
    // Since R13-B2, SCOPE_STALE and ABORTED answers from B mean the submit request was never made.
    const refused = outcome.reason === 'SIP_R7_SCOPE_STALE' || outcome.reason === 'SIP_R7_ABORTED' ||
      outcome.reason === 'SIP_R7_REQUEST_INVALID' || outcome.reason === 'SIP_R13B_RUN_IN_PROGRESS' ||
      (outcome.status >= 400 && outcome.status < 500)
    if (!accepted && !refused && m.run === run) run.uncertain = true
    if (run.current() && m.run === run && !run.terminal) {
      publish(run.current, { busy: accepted, projectPending: accepted
        ? { binding, authorityKey: run.authorityKey, tool: run.tool } : null })
    }
    // A never-sent refusal must also release a run whose capture rotated before this continuation.
    if (!accepted && refused && m.run === run) m.run = null
    return outcome
  }
  function complete(outcome, meta) {
    const run = m.run
    if (!entryCurrent() || !run || !run.current() || outcome?.state !== 'succeeded' ||
        run.outcome !== outcome || run.meta !== meta || !current(meta?.isCurrent) || meta.signal?.aborted) return Promise.resolve(false)
    if (m.transition?.outcome === outcome) return m.transition.wait.promise
    const t = { outcome, meta, binding: meta.binding, authorityKey: meta.authorityKey, client: a.projectClient,
      output: { ...meta.binding, input_version_id: outcome.outputVersionId },
      workspace: inputs.workspaceController, session: inputs.sessionController, identity: [...m.seen],
      epoch: m.epoch, phase: 'selection', selected: false, selecting: false, pending: true,
      retired: false, wait: deferred() }
    m.transition = t
    boundReadback(t)
    publish(() => transitionCurrent(t), { publishedVersionId: outcome.outputVersionId, busy: true })
    return selectOutput(t)
  }
  function retryReadback() {
    const t = m.transition
    if (!entryCurrent() || !t || !transitionCurrent(t)) return Promise.resolve(false)
    if (t.pending) return t.wait.promise
    if (t.phase === 'done') return Promise.resolve(true)
    t.wait = deferred()
    t.pending = true
    boundReadback(t)
    publish(() => transitionCurrent(t), { busy: true })
    if (!t.selected) return selectOutput(t)
    refresh(value => transitionCurrent(t) ? value + 1 : value)
    return t.wait.promise
  }
  function onOpenTool(row) {
    const owner = m.owner, live = latest.current
    if (!entryCurrent() || !live.hostEnabled || live.externalBusy || m.run ||
        !ownerCurrent(owner) || !catalogRows(live).includes(row)) return false
    publish(() => entryCurrent() && ownerCurrent(owner), { openTool: row, toolOwner: a.scope })
    return true
  }
  function onClose() { publish(entryCurrent, { openTool: null, toolOwner: null }) }
  function setVisibleLayers(value) {
    publish(contextCurrent, previous => ({ visibleLayers: typeof value === 'function' ? value(previous.visibleLayers) : value }))
  }
  const contextView = contextCurrent() ? state.context.data : null
  return { onProjectSubmit, complete, retryReadback, contextView,
    contextCurrent, openTool: selectionCurrent(state)
      ? catalogRows(inputs).find(row => row.name === state.openTool?.name) ?? state.openTool : null,
    onOpenTool, onClose, projectRunMemory: state.projectRunMemory,
    projectPending: state.projectPending, projectOutcome: state.projectOutcome,
    submissionSignal: m.owner?.abort.signal, busy: state.busy || !!inputs.externalBusy,
    notice: state.notice, publishedVersionId: state.publishedVersionId,
    canRetryReadback: !!(m.transition && !m.transition.pending && m.transition.phase !== 'done' &&
      entryCurrent() && transitionCurrent(m.transition)),
    visibleLayers: state.visibleLayers, setVisibleLayers }
}
