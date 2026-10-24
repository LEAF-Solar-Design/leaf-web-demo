import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import { IntegrationManualWorkflowRunStatus } from '../../../vendor/assistant-react/src/actions/IntegrationManualWorkflowRunStatus'
import { updateManualWorkflowStatus } from '../../../vendor/assistant-react/src/actions/manual-workflow-status-state'
import { IntegrationActionSnapshotV1, IntegrationManualWorkflowSnapshotV1, IntegrationManualWorkflowSnapshotV2,
  IntegrationManualWorkflowRunV2, IntegrationManualWorkflowCancellationRequestV1,
  IntegrationManualWorkflowCancellationResultV1 } from '../../../vendor/assistant-contract/src/integration'
import { UUID } from './client.js'
import { ManualWorkflowHistoryPanel } from './ManualWorkflowHistoryPanel.jsx'

let nextContext = 0
const childIdentity = (run) => JSON.stringify([run.run_id, run.connection_id, run.connector_ref, run.operation_ref,
  run.grant_revision, run.submission_digest, run.authority_binding_digest, run.created_at])
const workflowIdentity = (run) => JSON.stringify([run.run_id, run.workflow_ref.id, run.workflow_ref.version,
  run.workflow_ref.digest, run.submission_digest, run.authority_mode, run.authority_binding_digest, run.created_at])
const follows = (prior, next) => {
  if (workflowIdentity(prior) !== workflowIdentity(next) || next.revision < prior.revision) return false
  if (prior.requested_at !== undefined && next.requested_at !== prior.requested_at) return false
  if (prior.state === 'cancel_requested' && !['cancel_requested', 'cancelled'].includes(next.state)) return false
  if (['succeeded', 'cancelled'].includes(prior.state) && next.state !== prior.state) return false
  if (next.revision === prior.revision) {
    const normalized = IntegrationManualWorkflowRunV2.safeParse({ ...prior, schema_version: 2 })
    return normalized.success && JSON.stringify(normalized.data) === JSON.stringify(next)
  }
  return true
}

export function ManualWorkflowRunPanel({ client, projectId, signal, guard, onForbidden, displayPreferences, requestedRunId, admittedBinding }) {
  const [input, setInput] = useState('')
  const [selected, setSelected] = useState('')
  const [stepId, setStepId] = useState('')
  const [evidence, setEvidence] = useState(null)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [historySignal, setHistorySignal] = useState(signal)
  const binding = useRef(null)
  const feedback = useRef(null)
  const actions = useRef(null)
  actions.current = { operate }
  // S96 aborts when this callback changes, including during pending rerenders.
  const cancelWorkflow = useCallback((request, callbackSignal) => actions.current.operate('Cancel', undefined, request, callbackSignal), [])
  useLayoutEffect(() => {
    const ctx = { key: `workflow-${++nextContext}`, busy: false, history: undefined, selected: '', verified: new Map(), controller: new AbortController() }
    binding.current = ctx
    setHistorySignal(ctx.controller.signal)
    setInput(''); setSelected(''); setStepId(''); setEvidence(null); setPending(''); setError(''); setUncertain(false)
    const abort = () => {
      ctx.controller.abort(); ctx.history = undefined; ctx.anchor = null; ctx.verified.clear()
      setInput(''); setSelected(''); setStepId(''); setEvidence(null); setPending(''); setError(''); setUncertain(false)
    }
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    return () => { abort(); signal.removeEventListener('abort', abort); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])
  useLayoutEffect(() => { if (!pending && (error || evidence)) feedback.current?.focus() }, [error, evidence, pending])
  useLayoutEffect(() => {
    const ctx = binding.current
    if (!ctx || !requestedRunId || ctx.consumed === requestedRunId || ctx.controller.signal.aborted) return
    ctx.consumed = requestedRunId
    ctx.operation?.abort()
    ctx.historyOperation?.abort()
    ctx.historyOperation = undefined
    ctx.busy = false
    ctx.selected = ''
    ctx.history = undefined
    ctx.anchor = admittedBinding
    setEvidence(null); setInput(requestedRunId)
    operate('Load', requestedRunId)
  }, [client, projectId, signal, requestedRunId, admittedBinding])

  function selectSummary(value) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
    const parsed = IntegrationManualWorkflowRunV2.safeParse(value)
    if (!parsed.success) { setError('The selected run summary could not be verified.'); return }
    const run = parsed.data
    const verified = ctx.verified.get(run.run_id)
    const prior = ctx.anchor?.run_id === run.run_id && (!verified || ctx.anchor.revision > verified.revision) ? ctx.anchor : verified
    if (prior && (workflowIdentity(prior) !== workflowIdentity(run) ||
        (run.revision >= prior.revision && !follows(prior, run)))) {
      setError('The selected run summary could not be verified.'); return
    }
    ctx.anchor = prior && prior.revision > run.revision ? prior : run
    ctx.verified.set(run.run_id, ctx.anchor)
    setInput(run.run_id)
    operate('Load', run.run_id)
  }

  async function operate(kind, requestedId, cancellation, callbackSignal) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted || callbackSignal?.aborted ||
        (ctx.uncertain && ['Resume', 'Reconcile', 'Cancel'].includes(kind))) {
      if (kind === 'Cancel') throw new Error('unavailable')
      return
    }
    const runId = requestedId || (kind === 'Load' ? input.trim() : ctx.selected)
    if (!UUID.test(runId)) { setError('Enter a valid run UUID.'); return }
    const state = ctx.history?.snapshot.run.state
    if (kind === 'Resume' && ['succeeded', 'failed', 'cancel_requested', 'cancelled'].includes(state)) return
    if (kind === 'Reconcile' && ['succeeded', 'failed', 'cancelled'].includes(state)) return
    if (kind === 'Reconcile' && !ctx.history?.snapshot.steps.some((step) => step.step_id === stepId)) return
    if (kind === 'Reconcile' && state === 'cancel_requested' &&
        !ctx.history.snapshot.steps.some((step) => step.step_id === stepId && step.state === 'action')) return
    if (kind === 'Cancel') {
      const request = IntegrationManualWorkflowCancellationRequestV1.parse(cancellation)
      const run = ctx.history?.snapshot.run
      if (!run || run.schema_version !== 2 || !['accepted', 'running', 'uncertain'].includes(run.state) ||
          request.run_id !== runId || request.expected_revision !== run.revision) throw new Error('invalid')
    }
    ctx.busy = true
    if (runId !== ctx.selected) {
      if (!requestedId) ctx.anchor = null
      ctx.selected = runId
      ctx.key = `workflow-${++nextContext}`
      ctx.history = undefined
      setEvidence(null)
      setStepId('')
      if (!ctx.uncertain) setUncertain(false)
    }
    setSelected(runId)
    setPending(kind)
    setError('')
    let mutationStarted = kind === 'Cancel'
    const controller = new AbortController()
    ctx.operation = controller
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    callbackSignal?.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted && ctx.operation === controller
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    let timer
    let cancel
    try {
      const work = async () => {
        await guard(controller.signal)
        assertCurrent()
        let result
        let cancellationResult
        let resumedHistory
        let reconciledChild
        if (kind === 'Resume') {
          mutationStarted = true
          const resumed = IntegrationManualWorkflowSnapshotV1.parse(await client.resumeManualWorkflowRun(projectId, runId, {}, { signal: controller.signal }))
          assertCurrent()
          if (resumed.run.run_id !== runId || workflowIdentity(resumed.run) !== workflowIdentity(ctx.history.snapshot.run) ||
              resumed.run.revision < ctx.history.snapshot.run.revision) throw new Error('invalid')
          resumedHistory = updateManualWorkflowStatus(undefined, ctx.key, resumed)
          await guard(controller.signal)
          assertCurrent()
          result = await client.getManualWorkflowRunV2(projectId, runId, { signal: controller.signal })
          if (result.run?.revision < resumed.run.revision || workflowIdentity(result.run) !== workflowIdentity(resumed.run)) throw new Error('invalid')
        } else if (kind === 'Cancel') {
          mutationStarted = true
          cancellationResult = IntegrationManualWorkflowCancellationResultV1.parse(await client.cancelManualWorkflowRun(projectId, runId, cancellation, { signal: controller.signal }))
          result = cancellationResult.snapshot
        } else {
          if (kind === 'Reconcile') {
            const prior = ctx.history.snapshot.steps.find((step) => step.step_id === stepId)
            mutationStarted = true
            const child = IntegrationActionSnapshotV1.parse(await client.reconcileManualWorkflowStepV2(projectId, runId, stepId, {}, { signal: controller.signal }))
            assertCurrent()
            if (prior.state === 'action' && (childIdentity(child.run) !== childIdentity(prior.action.run) ||
                child.run.revision < prior.action.run.revision)) throw new Error('invalid')
            reconciledChild = child
            // The child result is never used as parent-bound display evidence.
            await guard(controller.signal)
            assertCurrent()
          }
          result = await client.getManualWorkflowRunV2(projectId, runId, { signal: controller.signal })
        }
        assertCurrent()
        await guard(controller.signal)
        assertCurrent()
        const parsed = IntegrationManualWorkflowSnapshotV2.parse(result)
        if (parsed.run.run_id !== runId) throw new Error('invalid')
        if (reconciledChild) {
          const step = parsed.steps.find((item) => item.step_id === stepId)
          if (step?.state !== 'action' || childIdentity(step.action.run) !== childIdentity(reconciledChild.run) ||
              step.action.run.revision < reconciledChild.run.revision) throw new Error('invalid')
        }
        if (ctx.anchor && !follows(ctx.anchor, parsed.run)) throw new Error('invalid')
        const resumedDetail = resumedHistory ? updateManualWorkflowStatus(resumedHistory, ctx.key, parsed) : undefined
        if (resumedDetail?.unverified) throw new Error('invalid')
        const history = updateManualWorkflowStatus(ctx.history, ctx.key, resumedDetail?.snapshot || parsed)
        if (!history || history.unverified) throw new Error('invalid')
        ctx.history = history
        ctx.anchor = history.snapshot.run
        const prior = ctx.verified.get(runId)
        if (!prior || (workflowIdentity(prior) === workflowIdentity(history.snapshot.run) && history.snapshot.run.revision >= prior.revision)) {
          ctx.verified.set(runId, history.snapshot.run)
        }
        setEvidence(history.snapshot)
        if (kind === 'Refresh' || !ctx.uncertain) { ctx.uncertain = false; setUncertain(false) }
        return cancellationResult ? { ...cancellationResult, snapshot: history.snapshot } : undefined
      }
      return await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current()) { if (kind === 'Cancel') throw failure; return }
      if (failure?.status === 401 || failure?.status === 403) {
        setEvidence(null)
        ctx.history = undefined
        ctx.anchor = null; ctx.verified.clear()
        ctx.controller.abort()
        setPending('')
        onForbidden()
        if (kind === 'Cancel') throw failure
        return
      }
      if (mutationStarted) { ctx.uncertain = true; setUncertain(true) }
      setError(mutationStarted ? 'The workflow operation could not be verified. Refresh before continuing.' :
        'The workflow could not be verified. Check the run UUID and Refresh or Load again. Last verified evidence is retained.')
      if (kind === 'Cancel') throw new Error('Cancellation could not be verified. Refresh before continuing.')
    } finally {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      callbackSignal?.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending('') }
    }
  }
  const terminal = evidence && ['succeeded', 'failed', 'cancelled'].includes(evidence.run.state)
  const cancelRequested = evidence?.run.state === 'cancel_requested'
  return <section aria-label="Existing manual workflow" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h2>Existing manual workflow</h2>
    <p>Load an existing run. Refresh may record recovery state but does not dispatch provider work. Resume may execute later steps. Reconcile checks a persisted step and never resumes the workflow.</p>
    <ManualWorkflowHistoryPanel client={client} projectId={projectId} signal={historySignal} guard={guard}
      acquire={(controller) => {
        const ctx = binding.current
        if (!ctx || ctx.busy || ctx.controller.signal.aborted) return false
        ctx.historyOperation = controller
        ctx.busy = true; setPending('History'); return true
      }} release={(controller) => {
        const ctx = binding.current
        if (ctx && ctx.controller.signal === historySignal && !ctx.controller.signal.aborted && ctx.historyOperation === controller) {
          ctx.historyOperation = undefined
          ctx.busy = false; setPending('')
        }
      }}
      onForbidden={() => {
        const ctx = binding.current
        if (ctx) { ctx.controller.abort(); ctx.history = undefined; ctx.anchor = null; ctx.verified.clear() }
        setEvidence(null); setPending(''); setError(''); setUncertain(false)
        onForbidden()
      }} onSelect={selectSummary} disabled={Boolean(pending)} displayPreferences={displayPreferences} />
    <form onSubmit={(event) => { event.preventDefault(); operate('Load') }}>
      <label>Run UUID<input style={{ minHeight: 44, maxWidth: '100%', boxSizing: 'border-box' }} value={input} maxLength={36} disabled={Boolean(pending)} onChange={(event) => setInput(event.target.value)} /></label>
      <button style={{ minHeight: 44 }} disabled={Boolean(pending)}>Load</button>
    </form>
    <div ref={feedback} tabIndex={-1}>
      <p role="status" aria-live="polite">{pending ? `${pending} in progress…` : evidence ? 'Run details verified.' : ''}</p>
      {uncertain && <p role="status">Outcome unknown. Work may have completed. Refresh to recover verified evidence.</p>}
      {error && <p role="alert">{error}</p>}
    </div>
    {selected && <button style={{ minHeight: 44 }} disabled={Boolean(pending)} onClick={() => operate('Refresh')}>Refresh</button>}
    {evidence && <>
      <IntegrationManualWorkflowRunStatus snapshot={evidence} contextKey={binding.current.key} displayPreferences={displayPreferences}
        onCancel={!uncertain && (!pending || pending === 'Cancel') ? cancelWorkflow : undefined} />
      {terminal && <p>This run is terminal. Refresh to check its evidence.</p>}
      <button style={{ minHeight: 44 }} disabled={Boolean(pending) || terminal || cancelRequested || uncertain} onClick={() => operate('Resume')}>Resume</button>
      <label>Persisted step<select style={{ minHeight: 44, maxWidth: '100%' }} value={stepId} disabled={Boolean(pending)} onChange={(event) => setStepId(event.target.value)}>
        <option value="">Select step</option>{evidence.steps.map((step, index) => <option key={step.step_id} value={step.step_id}>Step {index + 1} · {step.step_id}</option>)}
      </select></label>
      <button style={{ minHeight: 44 }} disabled={Boolean(pending) || !stepId || terminal || uncertain ||
        (cancelRequested && !evidence.steps.some((step) => step.step_id === stepId && step.state === 'action'))} onClick={() => operate('Reconcile')}>Reconcile</button>
    </>}
  </section>
}
