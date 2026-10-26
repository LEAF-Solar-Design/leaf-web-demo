import { useLayoutEffect, useRef, useState } from 'react'
import { humanizeError } from '../errorHumanize.js'
import { IntegrationRefV1, IntegrationManualWorkflowDefinitionV1, IntegrationManualWorkflowSubmissionV1,
  IntegrationManualWorkflowSubmissionResultV1 } from '../../../vendor/assistant-contract/src/integration'

const sameRef = (a, b) => ['id', 'version', 'digest'].every((key) => a[key] === b[key])
const authority = (connection) => JSON.stringify([connection.id, connection.state, connection.connector_ref.id,
  connection.connector_ref.version, connection.connector_ref.digest, [...connection.operation_ids].sort(), connection.grant_revision])

export function ManualWorkflowStartPanel({ client, projectId, signal, guard, onForbidden, onAdmitted }) {
  const [input, setInput] = useState({ id: '', version: '', digest: '' })
  const [pastedReference, setPastedReference] = useState('')
  const [review, setReview] = useState(null)
  const [pending, setPending] = useState('')
  const [unknown, setUnknown] = useState(false)
  const [locked, setLocked] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [resolved, setResolved] = useState(false)
  const binding = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), busy: false, review: null, intent: null }
    binding.current = ctx
    const reset = () => {
      setPastedReference('')
      setReview(null); setInput({ id: '', version: '', digest: '' }); setPending(''); setUnknown(false); setLocked(false); setError(''); setNotice(''); setResolved(false)
    }
    const clear = () => {
      ctx.controller.abort()
      ctx.review = null
      ctx.intent = null
      reset()
    }
    reset()
    signal.addEventListener('abort', clear, { once: true })
    if (signal.aborted) clear()
    return () => { ctx.controller.abort(); ctx.review = null; ctx.intent = null; signal.removeEventListener('abort', clear); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])

  async function operate(kind) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted || (ctx.intent && kind !== 'Recover')) return
    if (kind !== 'Load' && !ctx.review) return
    ctx.busy = true
    setPending(kind); setError(''); setNotice('')
    const controller = new AbortController()
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let dispatched = false
    let timer, cancel
    try {
      const work = async () => {
        await guarded()
        let candidate = ctx.review
        if (kind === 'Load') {
          ctx.review = null; setReview(null)
          const reference = IntegrationRefV1.parse({ id: input.id, version: Number(input.version), digest: input.digest })
          const definition = IntegrationManualWorkflowDefinitionV1.parse(await client.getManualWorkflowDefinition(projectId, reference, { signal: controller.signal }))
          assertCurrent()
          if (definition.id !== reference.id || definition.version !== reference.version) throw new Error('invalid')
          candidate = { reference, definition }
        }
        const connections = new Map()
        for (const step of candidate.definition.steps) {
          if (!connections.has(step.connection_id)) {
            const connection = await client.getConnection(projectId, step.connection_id, { signal: controller.signal })
            assertCurrent()
            if (connection.id !== step.connection_id || connection.state !== 'active' ||
                !Number.isSafeInteger(connection.grant_revision) || connection.grant_revision <= 0 ||
                !Array.isArray(connection.operation_ids)) throw new Error('authority')
            connections.set(step.connection_id, connection)
          }
          const connection = connections.get(step.connection_id)
          if (!sameRef(connection.connector_ref, step.connector_ref) || !connection.operation_ids.includes(step.operation_ref.id)) throw new Error('authority')
        }
        const identities = [...connections].map(([id, connection]) => [id, authority(connection)])
        const grants = candidate.definition.steps.map((step) => ({ step_id: step.id,
          expected_grant_revision: connections.get(step.connection_id).grant_revision }))
        await guarded()
        if (kind === 'Load') {
          ctx.review = { ...candidate, identities, grants }
          setReview(ctx.review)
          return
        }
        if (JSON.stringify(identities) !== JSON.stringify(candidate.identities)) throw new Error('authority')
        await guarded()
        if (kind === 'Start') {
          const submission = IntegrationManualWorkflowSubmissionV1.parse({ schema_version: 1, workflow_ref: candidate.reference,
            idempotency_key: globalThis.crypto.randomUUID(), step_grants: candidate.grants })
          ctx.intent = JSON.stringify(submission)
          setLocked(true)
        }
        if (!ctx.intent) throw new Error('invalid')
        // No asynchronous work may intervene between the final scope check and dispatch.
        assertCurrent()
        dispatched = true
        const result = IntegrationManualWorkflowSubmissionResultV1.parse(await client.submitManualWorkflow(projectId,
          JSON.parse(ctx.intent), { signal: controller.signal }))
        assertCurrent()
        if (result.status === 'refused') {
          setResolved(true)
          setUnknown(false)
          setError('This workflow request was refused because its idempotency key conflicts. No other invocation details are available.')
          return
        }
        if (!sameRef(result.run.workflow_ref, candidate.reference)) throw new Error('invalid')
        await guarded()
        setUnknown(false); setResolved(true); setNotice('Workflow admitted. Inspect the existing workflow viewer for verified evidence.')
        onAdmitted(result.run)
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current()) return
      if (failure?.status === 401 || failure?.status === 403) {
        ctx.review = null; ctx.intent = null; ctx.controller.abort()
        setPastedReference('')
        setReview(null); setInput({ id: '', version: '', digest: '' }); setUnknown(false); setLocked(false)
        setPending(''); setResolved(false); setError(''); setNotice('')
        onForbidden()
        return
      }
      if (dispatched) setUnknown(true)
      setError(ctx.intent && !dispatched ? 'Connection permissions changed or could not be verified. Recover the original request when access is restored, or leave recovery below.' : dispatched ? 'The submission could not be verified.' :
        'The workflow or your permissions could not be verified. Load a new review before starting.')
    } finally {
      clearTimeout(timer)
      controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending('') }
    }
  }

  const disabled = Boolean(pending) || locked
  function pasteReference(value) {
    if (disabled) return
    setPastedReference(value)
    binding.current.review = null; setReview(null)
    try {
      const reference = IntegrationRefV1.parse(JSON.parse(value))
      setInput({ ...reference, version: String(reference.version) })
      setError('')
    } catch {
      setInput({ id: '', version: '', digest: '' })
      setError('Paste a complete workflow reference with a valid ID, version and digest copied from a saved workflow.')
    }
  }
  return <section className="integration-setup" aria-label="Start manual workflow" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h2>Start manual workflow</h2>
    <p>Review an exact immutable definition. Loading only reads; Start may execute the ordered actions with your current authority. Never enter credentials.</p>
    <form onSubmit={(event) => { event.preventDefault(); operate('Load') }}>
      <label>Paste reference<input disabled={disabled} value={pastedReference}
        onChange={(event) => pasteReference(event.target.value)} /></label>
      <div className="integration-setup-grid">{['id', 'version', 'digest'].map((key) => <label key={key}>
        {({ id: 'Workflow definition ID', version: 'Workflow version', digest: 'Workflow digest' })[key]}
        <input required type={key === 'version' ? 'number' : 'text'} min={key === 'version' ? '1' : undefined}
          step={key === 'version' ? '1' : undefined} maxLength={key === 'digest' ? 64 : 128} disabled={disabled} value={input[key]}
          onChange={(event) => { setInput({ ...input, [key]: event.target.value }); binding.current.review = null; setReview(null) }} />
      </label>)}</div>
      <button disabled={disabled}>Load workflow review</button>
    </form>
    {review && <section aria-label="Immutable workflow review">
      <h3>{review.definition.title}</h3>
      <p>{review.reference.id} · version {review.reference.version} · {review.reference.digest}</p>
      <ol>{review.definition.steps.map((step) => <li key={step.id}>
        <p>{step.id} · Connection {step.connection_id} · Grant revision {review.grants.find((grant) => grant.step_id === step.id).expected_grant_revision}</p>
        <p>Connector: {step.connector_ref.id} · {step.connector_ref.version} · {step.connector_ref.digest}</p>
        <p>Permitted operation: {step.operation_ref.id} · {step.operation_ref.version} · {step.operation_ref.digest}</p>
        <p>Fixed arguments: <code>{JSON.stringify(step.arguments)}</code></p>
      </li>)}</ol>
      <p>Server policy remains authoritative. This review does not grant permission.</p>
      <button disabled={disabled} onClick={() => operate('Start')}>Start workflow</button>
    </section>}
    <p role="status" aria-live="polite">{pending ? `${pending} in progress…` : notice}</p>
    {unknown && <><p role="status">Outcome unknown. Work may have completed. The original request is retained only in this page session.</p>
      <button disabled={Boolean(pending)} onClick={() => operate('Recover')}>Recover original request (may continue the original workflow)</button>
      <p>Leaving recovery does not cancel the original workflow. Check workflow history before starting again to avoid duplicate work.</p>
      <button disabled={Boolean(pending)} onClick={() => {
        const ctx = binding.current
        if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
        ctx.intent = null; ctx.review = null
        setPastedReference('')
        setReview(null); setInput({ id: '', version: '', digest: '' }); setLocked(false); setUnknown(false); setResolved(false); setError('')
        setNotice('Recovery left. The original workflow may still run. Check workflow history before starting another.')
      }}>Leave recovery without cancelling</button></>}
    {resolved && <button disabled={Boolean(pending)} onClick={() => {
      const ctx = binding.current
      if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
      ctx.intent = null; ctx.review = null
      setReview(null); setLocked(false); setResolved(false); setNotice(''); setError('')
    }}>Review a new workflow intent</button>}
    {error && <p role="alert">{humanizeError(error)}</p>}
  </section>
}
