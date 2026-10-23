import { useLayoutEffect, useRef, useState } from 'react'
import { IntegrationManualWorkflowRunStatus } from '../../../vendor/assistant-react/src/actions/IntegrationManualWorkflowRunStatus'
import { updateManualWorkflowStatus } from '../../../vendor/assistant-react/src/actions/manual-workflow-status-state'
import { IntegrationActionSnapshotV1, IntegrationManualWorkflowSnapshotV1, IntegrationManualWorkflowRunV1 } from '../../../vendor/assistant-contract/src/integration'
import { UUID } from './client.js'
import { ManualWorkflowHistoryPanel } from './ManualWorkflowHistoryPanel.jsx'

let nextContext = 0
const childIdentity = (run) => JSON.stringify([run.run_id, run.connection_id, run.connector_ref, run.operation_ref,
  run.grant_revision, run.submission_digest, run.authority_binding_digest, run.created_at])
const workflowIdentity = (run) => JSON.stringify([run.run_id, run.workflow_ref.id, run.workflow_ref.version,
  run.workflow_ref.digest, run.submission_digest, run.authority_mode, run.authority_binding_digest, run.created_at])

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
    const parsed = IntegrationManualWorkflowRunV1.safeParse(value)
    if (!parsed.success) { setError('The selected run summary could not be verified.'); return }
    const run = parsed.data
    const verified = ctx.verified.get(run.run_id)
    const prior = ctx.anchor?.run_id === run.run_id && (!verified || ctx.anchor.revision > verified.revision) ? ctx.anchor : verified
    if (prior && workflowIdentity(prior) !== workflowIdentity(run)) {
      setError('The selected run summary could not be verified.'); return
    }
    ctx.anchor = prior && prior.revision > run.revision ? prior : run
    ctx.verified.set(run.run_id, ctx.anchor)
    setInput(run.run_id)
    operate('Load', run.run_id)
  }

  async function operate(kind, requestedId) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
    const runId = requestedId || (kind === 'Load' ? input.trim() : ctx.selected)
    if (!UUID.test(runId)) { setError('Enter a valid run UUID.'); return }
    if (kind === 'Reconcile' && !ctx.history?.snapshot.steps.some((step) => step.step_id === stepId)) return
    ctx.busy = true
    if (runId !== ctx.selected) {
      if (!requestedId) ctx.anchor = null
      ctx.selected = runId
      ctx.key = `workflow-${++nextContext}`
      ctx.history = undefined
      setEvidence(null)
      setStepId('')
      setUncertain(false)
    }
    setSelected(runId)
    setPending(kind)
    setError('')
    let mutationStarted = false
    const controller = new AbortController()
    ctx.operation = controller
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted && ctx.operation === controller
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    let timer
    let cancel
    try {
      const work = async () => {
        await guard(controller.signal)
        assertCurrent()
        let result
        if (kind === 'Resume') {
          mutationStarted = true
          result = await client.resumeManualWorkflowRun(projectId, runId, {}, { signal: controller.signal })
        } else {
          if (kind === 'Reconcile') {
            const prior = ctx.history.snapshot.steps.find((step) => step.step_id === stepId)
            mutationStarted = true
            const child = IntegrationActionSnapshotV1.parse(await client.reconcileManualWorkflowStep(projectId, runId, stepId, {}, { signal: controller.signal }))
            assertCurrent()
            if (prior.state === 'action' && childIdentity(child.run) !== childIdentity(prior.action.run)) throw new Error('invalid')
            // The child result is never used as parent-bound display evidence.
            await guard(controller.signal)
            assertCurrent()
          }
          result = await client.getManualWorkflowRun(projectId, runId, { signal: controller.signal })
        }
        assertCurrent()
        const parsed = IntegrationManualWorkflowSnapshotV1.parse(result)
        if (parsed.run.run_id !== runId) throw new Error('invalid')
        if (ctx.anchor && (workflowIdentity(parsed.run) !== workflowIdentity(ctx.anchor) || parsed.run.revision < ctx.anchor.revision)) throw new Error('invalid')
        const history = updateManualWorkflowStatus(ctx.history, ctx.key, parsed)
        if (!history || history.unverified) throw new Error('invalid')
        ctx.history = history
        const prior = ctx.verified.get(runId)
        if (!prior || (workflowIdentity(prior) === workflowIdentity(history.snapshot.run) && history.snapshot.run.revision >= prior.revision)) {
          ctx.verified.set(runId, history.snapshot.run)
        }
        setEvidence(history.snapshot)
        setUncertain(false)
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current()) return
      if (failure?.status === 401 || failure?.status === 403) {
        setEvidence(null)
        ctx.history = undefined
        ctx.anchor = null; ctx.verified.clear()
        ctx.controller.abort()
        setPending('')
        onForbidden()
        return
      }
      if (mutationStarted) setUncertain(true)
      setError(mutationStarted ? 'The workflow operation could not be verified. Refresh before continuing.' :
        'The workflow could not be verified. Check the run UUID and Refresh or Load again. Last verified evidence is retained.')
    } finally {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending('') }
    }
  }
  const terminal = evidence && ['succeeded', 'failed'].includes(evidence.run.state)
  return <section aria-label="Existing manual workflow" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h2>Existing manual workflow</h2>
    <p>Load an existing run. Refresh may record recovery state but does not dispatch provider work. Resume may execute later steps. Reconcile checks a persisted step and never resumes the workflow.</p>
    <ManualWorkflowHistoryPanel client={client} projectId={projectId} signal={historySignal} guard={guard}
      onForbidden={() => {
        const ctx = binding.current
        if (ctx) { ctx.controller.abort(); ctx.history = undefined; ctx.anchor = null; ctx.verified.clear() }
        setEvidence(null); setPending(''); setError(''); setUncertain(false)
        onForbidden()
      }} onSelect={selectSummary} disabled={Boolean(pending)} displayPreferences={displayPreferences} />
    <form onSubmit={(event) => { event.preventDefault(); operate('Load') }}>
      <label>Run UUID<input value={input} maxLength={36} disabled={Boolean(pending)} onChange={(event) => setInput(event.target.value)} /></label>
      <button disabled={Boolean(pending)}>Load</button>
    </form>
    <div ref={feedback} tabIndex={-1}>
      <p role="status" aria-live="polite">{pending ? `${pending} in progress…` : evidence ? 'Run details verified.' : ''}</p>
      {uncertain && <p role="status">Outcome unknown. Work may have completed. Refresh to recover verified evidence.</p>}
      {error && <p role="alert">{error}</p>}
    </div>
    {selected && <button disabled={Boolean(pending)} onClick={() => operate('Refresh')}>Refresh</button>}
    {evidence && <>
      <IntegrationManualWorkflowRunStatus snapshot={evidence} contextKey={binding.current.key} displayPreferences={displayPreferences} />
      {terminal && <p>This run is terminal. Refresh to check its evidence.</p>}
      <button disabled={Boolean(pending) || terminal || uncertain} onClick={() => operate('Resume')}>Resume</button>
      <label>Persisted step<select value={stepId} disabled={Boolean(pending)} onChange={(event) => setStepId(event.target.value)}>
        <option value="">Select step</option>{evidence.steps.map((step, index) => <option key={step.step_id} value={step.step_id}>Step {index + 1} · {step.step_id}</option>)}
      </select></label>
      <button disabled={Boolean(pending) || !stepId || terminal || uncertain} onClick={() => operate('Reconcile')}>Reconcile</button>
    </>}
  </section>
}
