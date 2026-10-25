import { useEffect, useRef, useState } from 'react'
import { createSolarDrawingBinding, sameSolarDrawingBinding, solarDrawingBindingKey } from './solarDrawingBinding.js'
import { PROJECT_SOLAR_GRAPH_TOOLS, projectSolarGraphReason, parseProjectSolarInsertionPoint,
  deriveProjectSolarGraphFormState, buildProjectSolarGraphRequest, exactProjectSolarGraphPreview,
  projectSolarGraphFeedback, proposePanelGroups, validatePanelGroups } from './solarProjectGraphFormModel.js'

function labelFor(tool) {
  switch (tool) {
    case 'solar-panels-from-drawing': return 'Import panels from drawing'
    case 'solar-panel-groups': return 'Panel groups'
    case 'solar-homeruns': return 'Homeruns'
    case 'solar-feeders': return 'Feeders'
    case 'solar-schedule': return 'Schedule'
    default: return 'Solar'
  }
}

export default function ProjectSolarGraphForm({ tool, solarBinding, authorityKey, projectClient,
  catalogDigest, newProjectIntentKey, onProjectSubmit, isCurrent, busy = false,
  projectOutcome = null, submissionSignal, onClose }) {
  const name = tool?.name
  const latest = useRef(null)
  latest.current = { name, solarBinding, authorityKey, projectClient, catalogDigest, newProjectIntentKey,
    onProjectSubmit, isCurrent, busy, submissionSignal }
  let binding = null
  let bindingKey = null
  try { binding = createSolarDrawingBinding(solarBinding); bindingKey = solarDrawingBindingKey(binding) }
  catch { /* The form renders the binding refusal. */ }
  const owner = useRef(null)
  if (!owner.current || owner.current.bindingKey !== bindingKey || owner.current.authorityKey !== authorityKey
      || owner.current.client !== projectClient || owner.current.name !== name) {
    if (owner.current) {
      owner.current.alive = false
      for (const controller of owner.current.controllers) controller.abort()
    }
    owner.current = { binding, bindingKey, authorityKey, client: projectClient, name,
      alive: true, generation: 0, controllers: new Set(), armed: null, candidate: null,
      lock: null, accepted: null, blocked: false, digest: catalogDigest, coordinateKey: null }
  }
  const scope = owner.current
  const [draft, setDraft] = useState(null)
  const [panelDraft, setPanelDraft] = useState(null)
  const [panelContext, setPanelContext] = useState(null)
  const panelInputs = panelDraft?.scope === scope ? panelDraft : { names: {}, numbers: {} }
  const panelGraph = panelContext?.scope === scope ? panelContext.graph : null
  const panelProposals = proposePanelGroups(panelGraph)
  const panelInputKey = name === 'solar-panel-groups' ? JSON.stringify([panelInputs.names, panelInputs.numbers]) : ''
  function groupInputs(proposals) {
    return proposals.map((proposal, index) => ({ key: proposal.key,
      name: panelInputs.names[proposal.key] ?? `Group ${index + 1}` }))
  }
  const proposalKey = (proposals) => JSON.stringify(proposals.map(({ key, panel_refs }) => [key, panel_refs]))
  const insertionPointText = draft?.scope === scope ? draft.text : ''
  const parsed = name === 'solar-schedule' ? parseProjectSolarInsertionPoint(insertionPointText) : null
  const coordinateKey = name === 'solar-schedule' ? (parsed?.ok ? JSON.stringify(parsed.point) : null) : panelInputKey
  const [refresh, setRefresh] = useState(0)
  const [view, setView] = useState(null)
  const [, repaint] = useState(0)
  const invalidate = (inputOnly = false) => {
    // Input edits expire previews, but leave the unchanged context usable at the new generation.
    if (inputOnly && scope.keptContext?.generation === scope.generation) scope.keptContext.generation += 1
    else scope.keptContext = null
    if (!inputOnly) scope.previewInputs = null
    scope.generation += 1
    scope.armed = null
    for (const controller of scope.controllers) controller.abort()
  }
  if (scope.digest !== catalogDigest || scope.coordinateKey !== coordinateKey) {
    invalidate(scope.digest === catalogDigest)
    scope.digest = catalogDigest
    scope.coordinateKey = coordinateKey
  }
  const available = binding && PROJECT_SOLAR_GRAPH_TOOLS.includes(name)
    && typeof authorityKey === 'string' && authorityKey.trim().length > 0
    && new TextEncoder().encode(authorityKey).byteLength <= 256
    && projectClient && ['readContext', 'prepareRun', 'catalog', 'captureCatalogScope', 'getSnapshot', 'subscribe']
      .every((key) => typeof projectClient[key] === 'function')
    && typeof newProjectIntentKey === 'function' && typeof onProjectSubmit === 'function' && typeof isCurrent === 'function'
  function scopeCurrent() {
    try {
      const props = latest.current
      const snapshot = scope.client?.getSnapshot?.()
      return scope.alive && owner.current === scope && props.projectClient === scope.client
        && props.authorityKey === scope.authorityKey && sameSolarDrawingBinding(props.solarBinding, scope.binding)
        && sameSolarDrawingBinding(snapshot?.binding, scope.binding) && snapshot?.authorityKey === scope.authorityKey
        && props.isCurrent() === true
    } catch { return false }
  }
  function setCurrent(next, generation = scope.generation) {
    if (scopeCurrent() && scope.generation === generation) setView({ scope, generation, ...next })
  }
  function operation() {
    const generation = scope.generation
    const checkoutCurrent = projectClient.captureCatalogScope()
    const controller = new AbortController()
    scope.controllers.add(controller)
    return { controller, signal: controller.signal, checkoutCurrent, generation,
      current: () => {
        try { return scopeCurrent() && scope.generation === generation && !controller.signal.aborted
          && latest.current.catalogDigest === catalogDigest && checkoutCurrent() === true }
        catch { return false }
      },
      finish: () => scope.controllers.delete(controller) }
  }
  const currentScope = scopeCurrent()
  if (available && !currentScope && !scope.staleSeen) {
    invalidate()
    scope.staleSeen = true
  } else if (currentScope) scope.staleSeen = false

  useEffect(() => {
    scope.alive = true
    const unsubscribe = available ? projectClient.subscribe(() => {
      if (!scope.alive || owner.current !== scope) return
      let checkoutCurrent = true
      try { if (scope.previewCurrent) checkoutCurrent = scope.previewCurrent() === true }
      catch { checkoutCurrent = false }
      if (!scopeCurrent() || !checkoutCurrent) {
        invalidate()
        scope.previewCurrent = null
        setView({ scope, generation: scope.generation, reason: 'scope_stale' })
        repaint((value) => value + 1)
      }
    }) : null
    return () => {
      scope.alive = false
      invalidate()
      unsubscribe?.()
    }
  }, [scope, Boolean(available)])

  useEffect(() => {
    const previous = scope.previewInputs
    scope.previewInputs = { coordinateKey, catalogDigest, busy, refresh, available: Boolean(available) }
    const inputOnly = previous && previous.coordinateKey !== coordinateKey
      && previous.catalogDigest === catalogDigest && previous.busy === busy && previous.refresh === refresh
      && previous.available === Boolean(available)
    if (!available || busy || scope.lock || scope.blocked || !scopeCurrent()) {
      scope.keptContext = null
      return undefined
    }
    const kept = scope.keptContext
    const reuseContext = inputOnly && kept?.generation === scope.generation
      && kept.catalogDigest === catalogDigest && kept.refresh === refresh
    if (!reuseContext) scope.keptContext = null
    scope.armed = null
    const pending = operation()
    scope.previewCurrent = pending.checkoutCurrent
    if (!pending.current()) { pending.finish(); return undefined }
    setCurrent({ reason: 'context_loading' }, pending.generation)
    let phase = 'context'
    const runPreview = async () => {
      try {
        if (!pending.current() || latest.current.busy) return
        const outcome = reuseContext ? kept.outcome : await projectClient.readContext({ signal: pending.signal })
        if (!pending.current() || latest.current.busy) return
        if (!reuseContext && outcome?.ok === true) {
          scope.keptContext = { outcome, generation: pending.generation, catalogDigest, refresh }
        }
        const state = outcome?.ok === true ? deriveProjectSolarGraphFormState({ binding, contextView: outcome.data })
          : { mode: 'refused', reason: 'context_unreadable' }
        if (state.mode !== 'edit') { setCurrent({ reason: state.reason }, pending.generation); return }
        let groups
        let proposals
        if (name === 'solar-panel-groups') {
          const graph = outcome.data.intake.solar_design_graph
          setPanelContext({ scope, graph })
          proposals = proposePanelGroups(graph).proposals
          const checked = validatePanelGroups(graph, groupInputs(proposals), panelInputs.numbers)
          if (!checked.ok) { setCurrent({ state, reason: checked.reason }, pending.generation); return }
          groups = checked.value
        }
        const validate = buildProjectSolarGraphRequest({ tool: name, binding, state, currentState: state,
          insertionPointText, groups, catalogDigest, idempotencyKey: 'candidate-validation' })
        if (!validate.ok) { setCurrent({ state, reason: validate.reason }, pending.generation); return }
        phase = 'prepare'
        const fingerprint = JSON.stringify([validate.request, state.intakeSha256])
        let candidate = scope.candidate
        if (!candidate || candidate.fingerprint !== fingerprint) {
          const built = buildProjectSolarGraphRequest({ tool: name, binding, state, currentState: state,
            insertionPointText, groups, catalogDigest, idempotencyKey: latest.current.newProjectIntentKey() })
          if (!built.ok) { setCurrent({ state, reason: built.reason }, pending.generation); return }
          candidate = { fingerprint, state, proposalKey: proposals ? proposalKey(proposals) : null,
            intent: projectClient.prepareRun(built.request) }
          scope.candidate = candidate
        }
        if (!pending.current() || latest.current.busy) return
        setCurrent({ state, reason: 'preview_required' }, pending.generation)
        phase = 'preview'
        const preview = await projectClient.catalog({ intents: [candidate.intent], signal: pending.signal })
        if (!pending.current() || latest.current.busy) return
        const exact = exactProjectSolarGraphPreview({ tool: name, catalogDigest, outcome: preview })
        if (exact.accepted) {
          scope.armed = { candidate, current: pending.current, generation: pending.generation }
          setCurrent({ state, reason: null }, pending.generation)
        } else {
          const feedback = projectSolarGraphFeedback({ tool: name,
            submission: { ok: false, status: 409, reason: exact.reasonCode } })
          setCurrent({ state, reason: 'preview_refused', feedback }, pending.generation)
        }
      } catch {
        if (pending.current() && !latest.current.busy) {
          setCurrent({ reason: phase === 'context' ? 'context_unreadable'
            : phase === 'preview' ? 'preview_refused' : 'request_invalid' }, pending.generation)
        }
      } finally { pending.finish() }
    }
    const timer = inputOnly ? setTimeout(runPreview, 300) : null
    if (!inputOnly) runPreview()
    return () => {
      clearTimeout(timer)
      pending.controller.abort()
      pending.finish()
    }
  }, [scope, Boolean(available), coordinateKey, catalogDigest, busy, refresh])

  useEffect(() => {
    try {
      const accepted = scope.accepted
      const raw = projectOutcome?.outcome
      const meta = projectOutcome?.meta
      if (!accepted || !scopeCurrent() || !meta || !raw) return
      // The job id is read once: the match and the feedback below see the same value.
      const jobId = raw.jobId
      if (jobId !== accepted.jobId
          || !sameSolarDrawingBinding(meta.binding, binding) || meta.authorityKey !== authorityKey
          || meta.tool !== name || typeof meta.isCurrent !== 'function' || meta.signal?.aborted) return
      if (meta.isCurrent() !== true || accepted.isCurrent() !== true) return
      // Every other completion field is read once into plain data; an unreadable field is an unknown outcome.
      const completion = { jobId }
      let readable = true
      for (const key of ['state', 'reasonCode', 'outputVersionId']) {
        try { completion[key] = raw[key] } catch { readable = false }
      }
      const feedback = projectSolarGraphFeedback({ tool: name,
        completion: readable ? completion : { jobId, state: 'outcome_unknown' } })
      scope.armed = null
      // The receipt keeps the meta object, so every render asks its current predicate.
      setCurrent({ reason: feedback.state === 'stale' ? 'scope_stale' : null, feedback,
        receipt: { meta, accepted } })
    } catch { return }
  }, [scope, projectOutcome, scope.accepted?.jobId])

  async function submit(event) {
    event.preventDefault()
    const armed = scope.armed
    if (!available || latest.current.busy || scope.lock || scope.blocked || !scopeCurrent()
        || !armed || !armed.current()) return
    const lock = {}
    scope.lock = lock
    repaint((value) => value + 1)
    const pending = operation()
    let handedOff = false
    let submittedScopeCurrent = null
    try {
      const outcome = await projectClient.readContext({ signal: pending.signal })
      if (!pending.current() || latest.current.busy || scope.armed !== armed) return
      const currentState = outcome?.ok === true ? deriveProjectSolarGraphFormState({ binding, contextView: outcome.data })
        : { mode: 'refused', reason: 'context_unreadable' }
      let groups
      if (name === 'solar-panel-groups' && currentState.mode === 'edit') {
        const graph = outcome.data.intake.solar_design_graph
        const proposed = proposePanelGroups(graph)
        const checked = validatePanelGroups(graph, groupInputs(proposed.proposals), panelInputs.numbers)
        const reason = proposalKey(proposed.proposals) !== armed.candidate.proposalKey ? 'panel_groups_stale'
          : !checked.ok ? checked.reason : null
        if (reason) {
          scope.armed = null
          setCurrent({ reason }, pending.generation)
          return
        }
        groups = checked.value
      }
      const built = buildProjectSolarGraphRequest({ tool: name, binding, state: armed.candidate.state,
        currentState, insertionPointText, groups, catalogDigest, idempotencyKey: armed.candidate.intent.idempotencyKey })
      if (!built.ok) {
        scope.armed = null
        setCurrent({ reason: built.reason }, pending.generation)
        return
      }
      if (!pending.current() || latest.current.busy || typeof latest.current.onProjectSubmit !== 'function') return
      // This predicate and signal belong to the submitted scope. They do not depend on this
      // component's mounted lifetime, editable coordinates, or local operation controller.
      const guard = latest.current.isCurrent
      const clientGuard = projectClient.captureCatalogScope()
      submittedScopeCurrent = () => {
        try {
          const snapshot = projectClient.getSnapshot()
          return guard() === true && clientGuard() === true && sameSolarDrawingBinding(snapshot?.binding, binding)
            && snapshot?.authorityKey === authorityKey
        } catch { return false }
      }
      handedOff = true
      scope.blocked = true
      scope.armed = null
      const submission = await latest.current.onProjectSubmit(armed.candidate.intent,
        { binding, authorityKey, signal: latest.current.submissionSignal, isCurrent: submittedScopeCurrent })
      if (!scopeCurrent() || !submittedScopeCurrent()) return
      const feedback = projectSolarGraphFeedback({ tool: name, submission })
      if (feedback.state === 'submitted') scope.accepted = { jobId: feedback.jobId, isCurrent: submittedScopeCurrent }
      if (feedback.state === 'refused') scope.blocked = false
      setCurrent({ reason: null, feedback })
    } catch {
      if (scopeCurrent()) {
        if (handedOff) {
          if (typeof submittedScopeCurrent !== 'function' || !submittedScopeCurrent()) return
          scope.blocked = true
          scope.armed = null
          setCurrent({ reason: null, feedback: projectSolarGraphFeedback({ tool: name, submission: null }) })
        } else {
          scope.armed = null
          setCurrent({ reason: 'context_unreadable' })
        }
      }
    } finally {
      pending.finish()
      if (scope.lock === lock) scope.lock = null
      if (scope.alive && owner.current === scope) repaint((value) => value + 1)
    }
  }

  const currentView = currentScope && view?.scope === scope && view.generation === scope.generation ? view : null
  let receiptLive = true
  if (currentView?.receipt) {
    try {
      receiptLive = currentView.receipt.meta.isCurrent() === true && currentView.receipt.accepted.isCurrent() === true
    }
    catch { receiptLive = false }
  }
  const armed = scope.armed && scope.armed.current()
  const reason = !PROJECT_SOLAR_GRAPH_TOOLS.includes(name) ? 'tool_unsupported' : !binding ? 'binding_invalid'
    : !available ? 'submit_unavailable' : !currentScope ? 'scope_stale'
      : !receiptLive ? 'scope_stale'
        : busy || scope.lock ? 'run_in_progress'
          : currentView?.reason ?? (!armed && !scope.blocked ? 'preview_required' : null)
  function close() {
    invalidate()
    scope.alive = false
    onClose?.()
  }
  return <section aria-label={labelFor(name)} data-testid="project-solar-graph-form">
    <h3>{labelFor(name)}</h3>
    {currentView?.state && <p>Graph revision {currentView.state.rev}</p>}
    {name === 'solar-panel-groups' && <fieldset disabled={busy || Boolean(scope.lock) || scope.blocked}>
      <legend>Proposed panel groups</legend>
      {panelProposals.proposals.map((proposal, index) => <div key={proposal.key}>
        <label>Group {index + 1} name
          <input type="text" value={panelInputs.names[proposal.key] ?? `Group ${index + 1}`}
            onChange={(event) => {
              invalidate(true)
              setPanelDraft({ scope, names: { ...panelInputs.names, [proposal.key]: event.target.value },
                numbers: panelInputs.numbers })
            }} />
        </label>
        <span>{proposal.count} panels, angle {proposal.angle_key}°</span>
      </div>)}
      {[
        ['module_width_along_row', 'Module width along row'],
        ['module_height_across_row', 'Module height across row'],
        ['alignment_tolerance', 'Alignment tolerance'],
      ].map(([key, label]) => <label key={key}>{label}
        <input type="number" required min="0" max="1000000" step="any" value={panelInputs.numbers[key] ?? ''}
          onChange={(event) => {
            invalidate(true)
            setPanelDraft({ scope, names: panelInputs.names, numbers: { ...panelInputs.numbers, [key]: event.target.value } })
          }} />
      </label>)}
      <p role="status">{panelProposals.singletons} singleton panels cannot be grouped.</p>
      {panelProposals.invalid_handles > 0 && <p role="status">
        {panelProposals.invalid_handles} panels have a drawing handle that cannot be grouped.
      </p>}
    </fieldset>}
    {name === 'solar-schedule' && <label>
      Insertion point (x, y, optional z)
      <input type="text" maxLength={160} value={insertionPointText} disabled={busy || Boolean(scope.lock) || scope.blocked}
        onChange={(event) => {
          const text = event.target.value
          const point = parseProjectSolarInsertionPoint(text)
          const nextKey = point.ok ? JSON.stringify(point.point) : null
          if (nextKey !== scope.coordinateKey) invalidate(true)
          setDraft({ scope, text })
        }} />
    </label>}
    {reason && <p role="status">{projectSolarGraphReason(reason, name)}</p>}
    {receiptLive && currentView?.feedback && <p role="status" data-testid="project-solar-graph-feedback">
      {currentView.feedback.text}
      {currentView.feedback.reasonCode && <span> ({currentView.feedback.reasonCode})</span>}
    </p>}
    <button type="button" disabled={Boolean(reason) || !armed || scope.blocked} onClick={submit}>Review &amp; run</button>
    <button type="button" disabled={!available || busy || Boolean(scope.lock) || scope.blocked} onClick={() => {
      if (latest.current.busy || scope.lock || scope.blocked || !scopeCurrent()) return
      invalidate()
      setRefresh((value) => value + 1)
    }}>Refresh preview</button>
    <button type="button" onClick={close}>Close</button>
  </section>
}
