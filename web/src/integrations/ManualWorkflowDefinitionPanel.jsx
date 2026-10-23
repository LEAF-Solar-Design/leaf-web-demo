import { useLayoutEffect, useRef, useState } from 'react'
import { IntegrationActionCatalogV1, IntegrationManualWorkflowDefinitionV1, IntegrationRefV1 } from '../../../vendor/assistant-contract/src/integration'
import { boundedIntegrationBody } from './client.js'

const blank = () => ({ id: '', title: '', version: '1', steps: [{ key: 1, id: 'step-1', action: null, fields: {} }] })
const actionKey = (action) => JSON.stringify([action.connection_id, action.connector_ref, action.operation_ref])

function definitionFrom(draft) {
  return IntegrationManualWorkflowDefinitionV1.parse({ schema_version: 1, id: draft.id, title: draft.title,
    version: Number(draft.version), trigger: 'manual', authority_mode: 'caller_bound', steps: draft.steps.map((step) => {
      if (!step.action) throw new Error('Choose a catalog action for every step.')
      const args = {}
      for (const field of step.action.form.fields) {
        const input = step.fields[field.id]
        if (input.mode === 'omit') {
          if (field.required) throw new Error('Required field omitted.')
          continue
        }
        if (input.mode === 'null') {
          if (!field.nullable) throw new Error('Field cannot be null.')
          args[field.id] = null
          continue
        }
        let value = input.value
        if (field.kind === 'number') {
          if (!value.trim()) throw new Error('Enter a number.')
          value = Number(value)
          if (!Number.isFinite(value) || (field.integer && !Number.isSafeInteger(value)) ||
              (field.minimum !== undefined && value < field.minimum) || (field.maximum !== undefined && value > field.maximum)) throw new Error('Number outside bounds.')
        } else if (field.kind === 'boolean') value = value === 'true'
        else if (field.kind === 'choice') {
          if (!field.choices.some((choice) => choice.value === value)) throw new Error('Choose a permitted value.')
        } else if (value.length < (field.min_length ?? 0) || value.length > (field.max_length ?? 4096)) throw new Error('Text outside bounds.')
        args[field.id] = value
      }
      return { id: step.id, kind: 'action', connection_id: step.action.connection_id,
        connector_ref: step.action.connector_ref, operation_ref: step.action.operation_ref, arguments: args }
    }) })
}

export function ManualWorkflowDefinitionPanel({ client, projectId, signal, guard, onForbidden }) {
  const [draft, setDraft] = useState(blank)
  const [catalog, setCatalog] = useState(null)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [unknown, setUnknown] = useState(false)
  const [result, setResult] = useState(null)
  const binding = useRef(null)
  const feedback = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), busy: false, intent: null, nextStep: 2, cursors: new Set() }
    binding.current = ctx
    const clear = () => {
      ctx.controller.abort(); ctx.intent = null
      setDraft(blank()); setCatalog(null); setPending(''); setError(''); setUnknown(false); setResult(null)
    }
    setDraft(blank()); setCatalog(null); setPending(''); setError(''); setUnknown(false); setResult(null)
    signal.addEventListener('abort', clear, { once: true })
    if (signal.aborted) clear()
    return () => { ctx.controller.abort(); ctx.intent = null; signal.removeEventListener('abort', clear); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])
  useLayoutEffect(() => { if (error || result) feedback.current?.focus() }, [error, result])

  async function operate(kind) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted || result || (ctx.intent && kind !== 'Retry')) return
    if (kind === 'Next' && (!catalog?.next_cursor || ctx.cursors.has(catalog.next_cursor) || ctx.cursors.size >= 20)) return
    ctx.busy = true; setPending(kind); setError('')
    const controller = new AbortController()
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let dispatched = false, conflict = false, timer, cancel
    try {
      const work = async () => {
        if (kind === 'Open' || kind === 'Next') {
          await guarded()
          const after = kind === 'Next' ? catalog.next_cursor : undefined
          const page = IntegrationActionCatalogV1.parse(await client.getActionCatalog(projectId, { signal: controller.signal, limit: 25, after }))
          assertCurrent(); await guarded()
          if (kind === 'Open') ctx.cursors.clear()
          if (after) ctx.cursors.add(after)
          setCatalog(page)
          return
        }
        const serialized = kind === 'Retry' ? ctx.intent : boundedIntegrationBody(definitionFrom(draft), 65536)
        if (!serialized) throw new Error('Missing original definition.')
        await guarded()
        ctx.intent = serialized
        assertCurrent()
        dispatched = true
        let response
        try {
          response = await client.registerManualWorkflow(projectId, JSON.parse(serialized), { signal: controller.signal })
        } catch (failure) {
          if (failure?.dispatched === false) dispatched = false
          conflict = failure?.status === 409
          throw failure
        }
        const reference = IntegrationRefV1.parse(response)
        assertCurrent()
        const definition = JSON.parse(serialized)
        if (reference.id !== definition.id || reference.version !== definition.version) throw new Error('Invalid reference.')
        await guarded()
        setUnknown(false); setResult({ reference, definition })
      }
      await Promise.race([work(), new Promise((_, reject) => {
        cancel = () => reject(new DOMException('Aborted', 'AbortError'))
        controller.signal.addEventListener('abort', cancel, { once: true })
        timer = setTimeout(() => { controller.abort(); reject(new Error('timeout')) }, 45000)
      })])
    } catch (failure) {
      if (!current()) return
      if (failure?.status === 401 || failure?.status === 403) {
        ctx.controller.abort(); ctx.intent = null
        setDraft(blank()); setCatalog(null); setPending(''); setError(''); setUnknown(false); setResult(null)
        onForbidden(); return
      }
      if (conflict) {
        ctx.intent = null; setUnknown(false)
        setError('This immutable ID and version conflict. Your input is retained. Deliberately change the ID or version before saving again.')
      } else if (dispatched || kind === 'Retry') {
        setUnknown(true)
        setError('Save outcome unknown. Retry only the identical definition retained in this page session.')
      } else {
        ctx.intent = null
        setError(kind === 'Open' || kind === 'Next' ? 'The action catalog could not be verified. Retry the catalog read.' :
          'The definition was not saved. Check the title, lowercase slug IDs, positive safe-integer version, unique steps and field bounds.')
      }
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending('') }
    }
  }

  const locked = Boolean(pending || unknown || result)
  const updateStep = (key, patch) => setDraft((old) => ({ ...old, steps: old.steps.map((step) => step.key === key ? { ...step, ...patch } : step) }))
  const move = (index, offset) => setDraft((old) => {
    const steps = [...old.steps]
    ;[steps[index], steps[index + offset]] = [steps[index + offset], steps[index]]
    return { ...old, steps }
  })
  return <section className="integration-setup" aria-label="Create manual workflow" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h2>Create manual workflow</h2>
    <p>Fields are saved business data. Credentials belong in connection setup. The host validates business semantics. Saving a definition does not start a run.</p>
    <button disabled={locked} onClick={() => operate('Open')}>Open action catalog</button>
    {catalog && <>
      {!catalog.actions.length && <p>No actions are available on this catalog page. Saving is disabled.</p>}
      {catalog.next_cursor && <button disabled={locked || binding.current?.cursors.has(catalog.next_cursor) || binding.current?.cursors.size >= 20}
        onClick={() => operate('Next')}>Next action catalog page</button>}
    </>}
    <form noValidate onSubmit={(event) => { event.preventDefault(); if (catalog?.actions.length) operate('Save') }}>
      <fieldset disabled={locked} style={{ minWidth: 0 }}><legend>Workflow definition</legend>
        <div className="integration-setup-grid">{['title', 'id', 'version'].map((field) => <label key={field}>
          {({ title: 'Definition title', id: 'Definition stable ID', version: 'Definition version' })[field]}
          <input style={{ maxWidth: '100%', minWidth: 0 }} value={draft[field]} type={field === 'version' ? 'number' : 'text'}
            min={field === 'version' ? 1 : undefined} step={field === 'version' ? 1 : undefined} maxLength={field === 'title' ? 120 : 128}
            onChange={(event) => setDraft({ ...draft, [field]: event.target.value })} />
        </label>)}</div>
        <ol>{draft.steps.map((step, index) => {
          const choices = [...(catalog?.actions || [])]
          if (step.action && !choices.some((action) => actionKey(action) === actionKey(step.action))) choices.unshift(step.action)
          return <li key={step.key}>
            <label>Step {index + 1} stable ID<input maxLength={128} value={step.id} onChange={(event) => updateStep(step.key, { id: event.target.value })} /></label>
            <label>Step {index + 1} action<select style={{ maxWidth: '100%' }} value={step.action ? actionKey(step.action) : ''} onChange={(event) => {
              const action = choices.find((entry) => actionKey(entry) === event.target.value)
              updateStep(step.key, { action: action || null, fields: Object.fromEntries((action?.form.fields || []).map((field) =>
                [field.id, { mode: field.required ? 'value' : 'omit', value: field.kind === 'boolean' ? 'false' : '' }])) })
            }}><option value="">Choose action</option>{choices.map((action) => <option key={actionKey(action)} value={actionKey(action)}>{action.title} · {action.connection_title} · {action.operation_ref.id}</option>)}</select></label>
            {step.action?.form.description && <p>{step.action.form.description}</p>}
            {step.action?.form.fields.map((field) => {
              const input = step.fields[field.id]
              const label = `Step ${index + 1} ${field.label}`
              const change = (patch) => updateStep(step.key, { fields: { ...step.fields, [field.id]: { ...input, ...patch } } })
              return <div key={field.id}>
                <label>{label} presence<select value={input.mode} onChange={(event) => change({ mode: event.target.value })}>
                  {!field.required && <option value="omit">Omit optional field</option>}<option value="value">Fixed value</option>
                  {field.nullable && <option value="null">Null</option>}
                </select></label>
                {input.mode === 'value' && <label>{label}{field.required ? ' (required)' : ''}
                  {field.kind === 'boolean' || field.kind === 'choice' ? <select value={input.value} onChange={(event) => change({ value: event.target.value })}>
                    {field.kind === 'boolean' ? <><option value="false">False</option><option value="true">True</option></> : <><option value="">Choose value</option>{field.choices.map((choice) => <option key={choice.value} value={choice.value}>{choice.label}</option>)}</>}
                  </select> : <input type={field.kind === 'number' ? 'number' : 'text'} value={input.value}
                    min={field.minimum} max={field.maximum} step={field.kind === 'number' ? field.integer ? 1 : 'any' : undefined}
                    maxLength={field.kind === 'text' ? field.max_length ?? 4096 : undefined}
                    onChange={(event) => change({ value: event.target.value })} />}
                </label>}
                {field.description && <p>{field.description}</p>}
              </div>
            })}
            <button type="button" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move step ${index + 1} up`}>Move up</button>
            <button type="button" disabled={index === draft.steps.length - 1} onClick={() => move(index, 1)} aria-label={`Move step ${index + 1} down`}>Move down</button>
            <button type="button" disabled={draft.steps.length === 1} aria-label={`Remove step ${index + 1}`} onClick={() => setDraft({ ...draft, steps: draft.steps.filter((entry) => entry.key !== step.key) })}>Remove</button>
          </li>
        })}</ol>
        <button type="button" disabled={draft.steps.length >= 8} onClick={() => {
          const key = binding.current.nextStep++
          setDraft({ ...draft, steps: [...draft.steps, { key, id: `step-${key}`, action: null, fields: {} }] })
        }}>Add step</button>
        <button disabled={!catalog?.actions.length}>Save workflow definition</button>
      </fieldset>
    </form>
    <div ref={feedback} tabIndex={-1} style={{ minWidth: 0 }}>
      <p role="status" aria-live="polite">{pending ? `${pending} in progress…` : result ? 'Workflow definition saved. Saving has not started a run.' :
        catalog ? `Action catalog loaded. ${catalog.actions.length} actions on this page.` : ''}</p>
      {error && <p role="alert">{error}</p>}
      {result && <section aria-label="Saved immutable definition">
        <p>Copy this immutable reference into the existing Start review.</p>
        {['id', 'version', 'digest'].map((field) => <label key={field}>Saved workflow {field}<input readOnly value={result.reference[field]} /></label>)}
        <ol>{result.definition.steps.map((step) => <li key={step.id}>{step.id} · {step.connection_id} · {step.operation_ref.id}<pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{JSON.stringify(step, null, 2)}</pre></li>)}</ol>
      </section>}
    </div>
    {unknown && <button disabled={Boolean(pending)} onClick={() => operate('Retry')}>Retry identical workflow definition</button>}
    {result && <button onClick={() => {
      const ctx = binding.current
      if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
      ctx.intent = null; ctx.nextStep = 2; ctx.cursors.clear()
      setDraft(blank()); setCatalog(null); setResult(null); setUnknown(false); setError('')
    }}>Compose a new definition</button>}
  </section>
}
