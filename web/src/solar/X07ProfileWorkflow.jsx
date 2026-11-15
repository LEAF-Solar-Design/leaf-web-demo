import { useEffect, useRef, useState } from 'react'
import X07RoleConfirmation from './X07RoleConfirmation'
import X07CopyPicker from './X07CopyPicker'
import X07CustomerProfileEditor from './X07CustomerProfileEditor'

// Offline composition only. All I/O is injected. drawingKey must include the
// immutable drawing version, so any context switch unmounts and retires saves.
export default function X07ProfileWorkflow(props) {
  const keys = [props.customerKey, props.projectKey, props.drawingKey]
  if (keys.some(value => typeof value !== 'string' || !value || value !== value.trim()))
    return <p role="alert">Choose a customer, project and drawing version.</p>
  return <Workflow key={JSON.stringify(keys)} {...props} />
}

function Workflow({ customerKey, projectKey, drawingKey, loadWorkflow, confirmRoles, applyProfile,
  loadProfile, saveProfile, onApplied, onCancel }) {
  const context = { customer_key: customerKey, project_key: projectKey, drawing_key: drawingKey }
  const [plan, setPlan] = useState(null), [choice, setChoice] = useState(null)
  const [result, setResult] = useState(null), [error, setError] = useState(null)
  const [cancelled, setCancelled] = useState(false), [loading, setLoading] = useState(true)
  const alive = useRef(false), retired = useRef(false), pending = useRef(null)
  const epoch = useRef(0)
  const callbacks = useRef(null)
  callbacks.current = { loadWorkflow, confirmRoles, applyProfile, onApplied, onCancel }
  const current = controller => alive.current && !retired.current && pending.current === controller && !controller.signal.aborted
  const scoped = value => value && Object.entries(context).every(([key, wanted]) => value[key] === wanted)
  const accept = value => {
    const pin = value?.applied_profile
    if (!scoped(value) || value.state !== 'applied' || pin?.customer_key !== customerKey
      || pin?.project_key !== projectKey || typeof pin.profile_id !== 'string' || !pin.profile_id
      || !Number.isSafeInteger(pin.version) || pin.version < 1) throw new Error('Invalid scoped application record.')
    setResult(value); callbacks.current.onApplied?.(value)
  }
  const operation = async (name, payload) => {
    if (!alive.current || retired.current || pending.current) throw new Error('Workflow retired or busy.')
    const controller = new AbortController(); pending.current = controller; setError(null)
    try {
      // A cancellation in the same event turn must prevent the producer call.
      await Promise.resolve()
      if (!current(controller)) return
      const value = await callbacks.current[name]({ ...context }, payload, { signal: controller.signal })
      if (current(controller)) accept(value)
      return value
    } finally { if (pending.current === controller) pending.current = null }
  }
  const load = () => {
    if (pending.current || retired.current) return
    const controller = new AbortController(); pending.current = controller
    const generation = epoch.current
    setLoading(true); setError(null)
    Promise.resolve().then(() => current(controller) ? callbacks.current.loadWorkflow({ ...context }, { signal: controller.signal }) : undefined)
      .then(value => {
        if (!current(controller)) return
        if (!scoped(value) || value.compatibility?.customer_key !== customerKey
          || !['apply', 'propose_fresh'].includes(value.compatibility.decision)) throw new Error('Invalid scoped workflow.')
        setPlan(value)
        pending.current = null
        if (value.compatibility.decision === 'apply' && value.copies?.length === 1)
          return operation('applyProfile', value.copies[0])
      }).catch(reason => { if (alive.current && !retired.current && epoch.current === generation) setError(reason?.message || 'Workflow could not be loaded.') })
      .finally(() => { if (pending.current === controller) pending.current = null; if (alive.current && epoch.current === generation) setLoading(false) })
  }
  useEffect(() => {
    alive.current = true; load()
    return () => { alive.current = false; epoch.current++; pending.current?.abort(); pending.current = null }
  }, [])
  const cancel = () => {
    if (retired.current) return
    retired.current = true; pending.current?.abort(); pending.current = null
    setCancelled(true); callbacks.current.onCancel?.({ ...context })
  }
  const candidates = plan?.copies?.filter(row => row.block?.toLowerCase() === choice?.block?.toLowerCase()
    && row.layer?.toLowerCase() === choice?.layer?.toLowerCase()) ?? []
  const confirm = selection => {
    const matching = plan?.copies?.filter(row => row.block?.toLowerCase() === selection.block.toLowerCase()
      && row.layer?.toLowerCase() === selection.layer.toLowerCase()) ?? []
    if (matching.length > 1) { setChoice(selection); return Promise.resolve() }
    return operation('confirmRoles', { ...selection, expected_version: plan.expected_version,
      ...(matching.length === 1 ? { design_window: matching[0].design_window } : {}) })
  }
  const pick = selection => operation(choice ? 'confirmRoles' : 'applyProfile', {
    ...selection, ...(choice ? { expected_version: plan.expected_version } : {}) })
  if (cancelled) return <p role="status">Profile workflow cancelled.</p>
  if (result) return <section aria-label="Applied drawing profile"><p role="status">Drawing profile applied.</p>
    <p>Applied version: {result.applied_profile.version}</p>
    {typeof loadProfile === 'function' && typeof saveProfile === 'function' && <X07CustomerProfileEditor
      customerKey={customerKey} loadProfile={loadProfile} saveProfile={saveProfile} applicationRecord={result.applied_profile} />}
  </section>
  return <section aria-label="Drawing profile workflow">
    {loading && <p role="status">Reading drawing profile.</p>}
    {error && <p role="alert">{error}</p>}
    {!loading && !plan && <button type="button" onClick={load}>Retry workflow</button>}
    {plan && (choice || plan.compatibility.decision === 'apply')
      ? <X07CopyPicker copies={choice ? candidates : plan.copies} onPick={pick} onCancel={cancel} />
      : plan && <X07RoleConfirmation proposal={plan.compatibility.proposal} onConfirm={confirm} onCancel={cancel} />}
    <button type="button" onClick={cancel}>Cancel workflow</button>
  </section>
}
