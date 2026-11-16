import { useEffect, useId, useRef, useState } from 'react'

const object = value => value && typeof value === 'object' && !Array.isArray(value)
const valid = (record, customer) => record?.customer_key === customer && typeof record.profile_id === 'string'
  && record.profile_id && Number.isSafeInteger(record.version) && record.version > 0 && object(record.roles)
  && object(record.roles.module) && typeof record.roles.module.block === 'string' && typeof record.roles.module.layer === 'string'
  && Object.entries(record.roles).every(([role, value]) => !['module', 'roof', 'obstruction'].includes(role)
    || (object(value) && typeof value.layer === 'string' && (value.block === undefined || typeof value.block === 'string')))
const clone = value => JSON.parse(JSON.stringify(value))

export default function X07CustomerProfileEditor(props) {
  if (typeof props.customerKey !== 'string' || !props.customerKey.trim()) return <p role="alert">Choose a customer to edit its profile.</p>
  return <Editor key={props.customerKey} {...props} />
}

function Editor({ customerKey, loadProfile, saveProfile, applicationRecord }) {
  const [profile, setProfile] = useState(null), [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(null)
  const [stale, setStale] = useState(false), [message, setMessage] = useState(null)
  const form = useRef(null), saveButton = useRef(null), reloadButton = useRef(null), retryButton = useRef(null)
  const composing = useRef(false)
  const errorId = useId(), focusRole = useRef(null)
  useEffect(() => {
    if (busy) return
    if (focusRole.current) {
      form.current?.querySelector(`[data-role-layer="${focusRole.current}"]`)?.focus()
      focusRole.current = null
    } else if (error) {
      if (!profile) retryButton.current?.focus()
      else if (stale) reloadButton.current?.focus()
      else {
        const input = [...(form.current?.querySelectorAll('input') ?? [])].find(node => !node.value.trim())
        ;(input || saveButton.current)?.focus()
      }
    } else if (message) saveButton.current?.focus()
  }, [busy, error, stale, message, draft])
  const alive = useRef(true), pending = useRef(null), callbacks = useRef({ loadProfile, saveProfile })
  callbacks.current = { loadProfile, saveProfile }
  const current = controller => alive.current && pending.current === controller && !controller.signal.aborted
  const load = keepEdits => {
    if (pending.current) return
    const restoreRetryFocus = document.activeElement === retryButton.current && retryButton.current !== null
    const controller = new AbortController(); pending.current = controller
    setBusy(true); setError(null)
    Promise.resolve().then(() => current(controller) ? callbacks.current.loadProfile(customerKey, { signal: controller.signal }) : undefined)
      .then(record => {
        if (!current(controller)) return
        if (!valid(record, customerKey)) throw new Error('invalid scoped record')
        const copy = clone(record.roles)
        if (restoreRetryFocus) focusRole.current = 'module'
        setProfile(record); if (!keepEdits) setDraft(copy)
        setStale(false)
      }).catch(() => { if (current(controller)) setError('The profile could not be loaded for this customer.') })
      .finally(() => { if (current(controller)) { pending.current = null; setBusy(false) } })
  }
  useEffect(() => {
    alive.current = true; load(false)
    return () => { alive.current = false; pending.current?.abort(); pending.current = null }
  }, [])
  const save = event => {
    event.preventDefault()
    if (composing.current || event.nativeEvent?.isComposing) return
    if (!profile || !draft || pending.current || stale) return
    if (!draft.module?.block?.trim() || !draft.module?.layer?.trim()) { setError('Module block and layer are required.'); return }
    if (['roof', 'obstruction'].some(role => draft[role] && !draft[role].layer?.trim())) {
      setError('Each added role needs a layer.'); return
    }
    const controller = new AbortController(); pending.current = controller
    setBusy(true); setError(null); setMessage(null)
    Promise.resolve().then(() => current(controller) ? callbacks.current.saveProfile(customerKey, clone(draft),
      { expected_version: profile.version, signal: controller.signal }) : undefined)
      .then(record => {
        if (!current(controller)) return
        if (record?.ok === false) throw record
        if (!valid(record, customerKey) || record.profile_id !== profile.profile_id || record.version <= profile.version) throw new Error('invalid saved record')
        const copy = clone(record.roles)
        setProfile(record); setDraft(copy); setMessage('Profile saved.')
      }).catch(reason => {
        if (!current(controller)) return
        const conflict = ['STALE_VERSION', 'VERSION_CONFLICT'].includes(reason?.code)
        setStale(conflict)
        setError(conflict ? 'The profile changed. Your edits are kept. Reload the version to retry.' : 'The profile could not be saved. Your edits are kept.')
      }).finally(() => { if (current(controller)) { pending.current = null; setBusy(false) } })
  }
  const edit = (role, field, value) => { setDraft(previous => ({ ...previous, [role]: { ...previous[role], [field]: value } })); setMessage(null) }
  const addRole = role => { focusRole.current = role; setDraft(previous => ({ ...previous, [role]: { layer: '' } })); setMessage(null); setError(null) }
  const removeRole = role => { focusRole.current = 'module'; setDraft(previous => { const next = { ...previous }; delete next[role]; return next }); setMessage(null); setError(null) }
  const pin = profile && applicationRecord?.customer_key === customerKey && applicationRecord.profile_id === profile.profile_id
    && Number.isSafeInteger(applicationRecord.version) && applicationRecord.version > 0 ? applicationRecord : null
  return <section aria-label="Customer role profile">
    <h3>Customer role profile</h3><p>Customer: {customerKey}</p>
    {profile && <><p>Profile id: {profile.profile_id}</p><p>Profile version: {profile.version}</p></>}
    {pin && <p>Project is pinned to version {pin.version}.</p>}
    {error && <p id={errorId} role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {!profile && !busy && <button ref={retryButton} type="button" onClick={() => load(false)}>Retry loading profile</button>}
    {draft && <form ref={form} onSubmit={save} aria-describedby={error ? errorId : undefined}
      onCompositionStart={() => { composing.current = true }}
      onCompositionEnd={() => { composing.current = false }}
      onKeyDown={event => { if (event.key === 'Enter' && (composing.current || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault() }}>
      <fieldset disabled={busy}><legend>Drawing roles</legend>
        {Object.keys(draft).filter(role => ['module', 'roof', 'obstruction'].includes(role)).map(role => {
          const label = role[0].toUpperCase() + role.slice(1)
          return <div key={role}>
            {(role === 'module' || typeof draft[role].block === 'string') && <label>{label} block<input value={draft[role].block || ''} onChange={event => edit(role, 'block', event.target.value)} /></label>}
            <label>{label} layer<input data-role-layer={role} aria-describedby={error ? errorId : undefined} value={draft[role].layer || ''} onChange={event => edit(role, 'layer', event.target.value)} /></label>
            {role !== 'module' && <button type="button" onClick={() => removeRole(role)}>Remove {role} role</button>}
          </div>
        })}
        {['roof', 'obstruction'].filter(role => !draft[role]).map(role =>
          <button key={role} type="button" onClick={() => addRole(role)}>Add {role} role</button>)}
      </fieldset>
      <button ref={saveButton} aria-describedby={error ? errorId : undefined} type="submit" disabled={busy || stale || typeof saveProfile !== 'function'}>Save profile</button>
      {stale && <button ref={reloadButton} type="button" disabled={busy} onClick={() => load(true)}>Reload version, keep edits</button>}
    </form>}
    {busy && <p role="status">Reading or saving profile.</p>}
  </section>
}
