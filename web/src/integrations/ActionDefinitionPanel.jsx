import { useLayoutEffect, useRef, useState } from 'react'
import { IntegrationActionCatalogV1, IntegrationActionElementV1 } from '../../../vendor/assistant-contract/src/integration'
import { boundedIntegrationBody } from './client.js'
import { humanizeError } from '../errorHumanize.js'

const blank = () => ({ id: '', title: '', action: null })
const actionKey = (action) => boundedIntegrationBody(action)
const canonical = (element) => boundedIntegrationBody(IntegrationActionElementV1.parse(element))

export function ActionDefinitionPanel({ client, projectId, signal, guard, onForbidden, onSaved }) {
  const [draft, setDraft] = useState(blank)
  const [catalog, setCatalog] = useState(null)
  const [pending, setPending] = useState('')
  const [error, setError] = useState('')
  const [recovery, setRecovery] = useState('')
  const [saved, setSaved] = useState(false)
  const binding = useRef(null)
  const abandoned = useRef(new Set())
  const feedback = useRef(null)
  useLayoutEffect(() => {
    const ctx = { controller: new AbortController(), busy: false, intent: null, cursors: new Set() }
    binding.current = ctx
    abandoned.current.clear()
    const clear = () => {
      ctx.controller.abort(); ctx.intent = null
      setDraft(blank()); setCatalog(null); setPending(''); setError(''); setRecovery(''); setSaved(false)
    }
    setDraft(blank()); setCatalog(null); setPending(''); setError(''); setRecovery(''); setSaved(false)
    signal.addEventListener('abort', clear, { once: true })
    if (signal.aborted) clear()
    return () => { ctx.controller.abort(); ctx.intent = null; signal.removeEventListener('abort', clear); if (binding.current === ctx) binding.current = null }
  }, [client, projectId, signal])
  useLayoutEffect(() => { if (error || saved) feedback.current?.focus() }, [error, saved])

  async function operate(kind) {
    const ctx = binding.current
    if (!ctx || ctx.busy || ctx.controller.signal.aborted || saved) return
    if (ctx.intent && kind !== 'Check' && !(kind === 'Retry' && recovery === 'missing')) return
    if ((kind === 'Check' || kind === 'Retry') && !ctx.intent) return
    if (kind === 'Next' && (!catalog?.next_cursor || ctx.cursors.has(catalog.next_cursor) || ctx.cursors.size >= 20)) return
    ctx.busy = true; setPending(kind); setError('')
    const controller = new AbortController()
    const abort = () => controller.abort()
    ctx.controller.signal.addEventListener('abort', abort, { once: true })
    const current = () => binding.current === ctx && !ctx.controller.signal.aborted
    const assertCurrent = () => { if (!current() || controller.signal.aborted) throw new DOMException('Aborted', 'AbortError') }
    const guarded = async () => { await guard(controller.signal); assertCurrent() }
    let dispatched = false, timer, cancel
    try {
      const work = async () => {
        if (kind === 'Load' || kind === 'Next') {
          await guarded()
          const after = kind === 'Next' ? catalog.next_cursor : undefined
          const page = IntegrationActionCatalogV1.parse(await client.getActionCatalog(projectId, { signal: controller.signal, limit: 25, after }))
          boundedIntegrationBody(page)
          assertCurrent(); await guarded()
          if (kind === 'Load') ctx.cursors.clear()
          if (after) ctx.cursors.add(after)
          setCatalog(page)
          return
        }
        let intended = ctx.intent
        if (!intended) {
          if (abandoned.current.has(draft.id)) throw Object.assign(new Error('Choose a new form ID. The previous form may already exist.'), { localValidation: true })
          if (!draft.action) throw new Error('Choose an action.')
          intended = canonical({ schema_version: 1, id: draft.id, title: draft.title.trim(), kind: 'integration.action',
            connection_id: draft.action.connection_id, connector_ref: draft.action.connector_ref,
            operation_ref: draft.action.operation_ref, form: draft.action.form })
        }
        await guarded()
        assertCurrent()
        let record
        if (kind === 'Check') {
          try { record = await client.getElement(projectId, JSON.parse(intended).id, { signal: controller.signal }) }
          catch (failure) {
            if (failure?.status !== 404) throw failure
            assertCurrent(); await guarded()
            setRecovery('missing'); setError('No saved form was found. You may explicitly retry creating the identical frozen form.')
            return
          }
        } else {
          ctx.intent = intended
          dispatched = true
          try { record = await client.saveElement(projectId, JSON.parse(intended), { signal: controller.signal }) }
          catch (failure) { if (failure?.dispatched === false && failure?.status !== 409) dispatched = false; throw failure }
        }
        assertCurrent(); await guarded()
        if (!Number.isSafeInteger(record?.revision) || record.revision < 1) throw new Error('Invalid saved record.')
        const actual = canonical(record.element)
        if (actual !== intended) {
          setRecovery('conflict'); setError('A different recipe exists for this ID. It has not been accepted or overwritten.')
          return
        }
        setRecovery(''); setSaved(true); onSaved(record)
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
        setDraft(blank()); setCatalog(null); setPending(''); setRecovery(''); setError('')
        onForbidden(); return
      }
      if (dispatched || kind === 'Check' || kind === 'Retry' || failure?.status === 409) {
        setRecovery('unknown'); setError('Save outcome unknown. Check the saved form before considering another create.')
      } else {
        ctx.intent = null
        setError(failure.localValidation ? humanizeError(failure) : kind === 'Load' || kind === 'Next' ? 'The action catalog is unavailable or could not be verified. Retry loading permitted actions.' :
          'The form was not saved. Enter a title, a stable lowercase slug ID, and choose a permitted action. Your draft is retained.')
      }
    } finally {
      clearTimeout(timer); controller.signal.removeEventListener('abort', cancel)
      ctx.controller.signal.removeEventListener('abort', abort)
      if (current()) { ctx.busy = false; setPending('') }
    }
  }

  const locked = Boolean(pending || recovery || saved)
  // Keep the exact selected entry even if the next page contains a changed entry for the same operation.
  const choices = [...(catalog?.actions || [])]
  if (draft.action && !choices.some((entry) => actionKey(entry) === actionKey(draft.action))) choices.unshift(draft.action)
  const uniqueChoices = [...new Map(choices.map((entry) => [actionKey(entry), entry])).values()]
  return <section className="integration-setup" aria-label="Create an action form" style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
    <h2>Create an action form</h2>
    <p>Choose a permitted action and save its reviewed form. Saving does not execute the action. Availability does not grant execution authority.</p>
    <button disabled={locked} onClick={() => operate('Load')}>Load permitted actions</button>
    {catalog && <>
      {!catalog.actions.length && <p>No permitted actions are available on this catalog page.</p>}
      {catalog.next_cursor && <button disabled={locked || binding.current?.cursors.has(catalog.next_cursor) || binding.current?.cursors.size >= 20}
        onClick={() => operate('Next')}>Next permitted actions page</button>}
      {catalog.next_cursor && (binding.current?.cursors.has(catalog.next_cursor) || binding.current?.cursors.size >= 20) &&
        <p>Catalog navigation stopped at a repeated cursor or the page limit.</p>}
    </>}
    <form noValidate onSubmit={(event) => { event.preventDefault(); operate('Save') }}>
      <fieldset disabled={locked} style={{ minWidth: 0 }}><legend>Action form</legend>
        <div className="integration-setup-grid">
          <label>Form title<input maxLength={120} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} /></label>
          <label>Form stable ID<input maxLength={128} value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value })} /></label>
          <label>Permitted action<select value={draft.action ? actionKey(draft.action) : ''} onChange={(event) =>
            setDraft({ ...draft, action: uniqueChoices.find((entry) => actionKey(entry) === event.target.value) || null })}>
            <option value="">Choose action</option>{uniqueChoices.map((entry) => <option key={actionKey(entry)} value={actionKey(entry)}>{entry.title} · {entry.connection_title} · {entry.operation_ref.id}</option>)}
          </select></label>
        </div>
        {draft.action && <div><p>{draft.action.form.description}</p><p>Connection: {draft.action.connection_id} · Operation: {draft.action.operation_ref.id} · version {draft.action.operation_ref.version}</p>
          <p>Reviewed fields: {draft.action.form.fields.map((field) => field.label).join(', ') || 'None'}</p></div>}
        <button disabled={!draft.action}>Save action form</button>
      </fieldset>
    </form>
    <div ref={feedback} tabIndex={-1} style={{ minWidth: 0 }}>
      <p role="status" aria-live="polite">{pending ? `${pending} in progress…` : saved ? 'Action form saved. No action was submitted.' :
        catalog ? `Permitted actions loaded. ${catalog.actions.length} actions on this page.` : ''}</p>
      {error && <p role="alert">{humanizeError(error)}</p>}
    </div>
    {recovery && <button disabled={Boolean(pending)} onClick={() => operate('Check')}>Check saved form</button>}
    {recovery === 'missing' && <button disabled={Boolean(pending)} onClick={() => operate('Retry')}>Retry identical action form</button>}
    {recovery && <p>The previous form may still exist. Leaving recovery does not delete it. Use a different form ID.</p>}
    {(saved || recovery) && <button disabled={Boolean(pending)} onClick={() => {
      const ctx = binding.current
      if (!ctx || ctx.busy || ctx.controller.signal.aborted) return
      if (ctx.intent) abandoned.current.add(JSON.parse(ctx.intent).id)
      ctx.intent = null
      setDraft(blank()); setSaved(false); setRecovery(''); setError('')
    }}>{saved ? 'Create another' : 'Leave recovery and create a different form'}</button>}
  </section>
}
