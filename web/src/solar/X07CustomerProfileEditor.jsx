import { useEffect, useId, useRef, useState } from 'react'
import X07ProfileRoleDiff from './X07ProfileRoleDiff.jsx'

const object = value => value && typeof value === 'object' && !Array.isArray(value)
const valid = (record, customer) => record?.customer_key === customer && typeof record.profile_id === 'string'
  && record.profile_id && Number.isSafeInteger(record.version) && record.version > 0 && object(record.roles)
  && object(record.roles.module) && typeof record.roles.module.block === 'string' && typeof record.roles.module.layer === 'string'
  && Object.entries(record.roles).every(([role, value]) => !['module', 'roof', 'obstruction'].includes(role)
    || (object(value) && typeof value.layer === 'string' && (value.block === undefined || typeof value.block === 'string')))
const clone = value => JSON.parse(JSON.stringify(value))
const roleNames = ['module', 'roof', 'obstruction']
const roleDiffers = (before, after, role) => Boolean(before[role]) !== Boolean(after[role])
  || ['block', 'layer'].some(field => Object.hasOwn(before[role] || {}, field) !== Object.hasOwn(after[role] || {}, field)
    || before[role]?.[field] !== after[role]?.[field])

export default function X07CustomerProfileEditor(props) {
  return <LeaveBoundary {...props} />
}

function LeaveBoundary(props) {
  const key = JSON.stringify([props.customerKey, props.drawingKey])
  const [held, setHeld] = useState({ key, props }), [message, setMessage] = useState(null)
  const dirty = useRef(false), attempt = useRef(null), sequence = useRef(0)
  useEffect(() => {
    if (held.key === key) return
    const epoch = ++sequence.current, controller = new AbortController()
    attempt.current?.abort();attempt.current = controller
    if (!dirty.current || typeof props.leaveGuard !== 'function') {
      dirty.current = false;setHeld({ key, props });setMessage(null);return
    }
    setMessage('Unsaved profile edits need a leave decision.')
    Promise.resolve().then(() => props.leaveGuard({ from: { customerKey: held.props.customerKey, drawingKey: held.props.drawingKey },
      to: { customerKey: props.customerKey, drawingKey: props.drawingKey }, dirty: true, signal: controller.signal }))
      .then(decision => {
        if (controller.signal.aborted || sequence.current !== epoch) return
        if (decision === 'discard') { dirty.current = false;setHeld({ key, props });setMessage(null) }
        else setMessage('Profile edits kept. Staying on the current drawing.')
      }).catch(() => { if (!controller.signal.aborted) setMessage('Profile edits kept. Leave decision failed.') })
    return () => controller.abort()
  }, [key, props.leaveAttempt])
  useEffect(() => () => attempt.current?.abort(), [])
  const active = held.key === key ? props : held.props
  if (typeof active.customerKey !== 'string' || !active.customerKey.trim()) return <p role="alert">Choose a customer to edit its profile.</p>
  return <>{message && <p role="status">{message}</p>}<Editor key={held.key} {...active} onDirtyChange={value => { dirty.current = value }} /></>
}

function Editor({ customerKey, loadProfile, saveProfile, applicationRecord, pinnedRoles, onDirtyChange }) {
  const [profile, setProfile] = useState(null), [draft, setDraft] = useState(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(null)
  const [stale, setStale] = useState(false), [message, setMessage] = useState(null)
  const [reconcile, setReconcile] = useState(false)
  const [undo, setUndo] = useState(null)
  const form = useRef(null), saveButton = useRef(null), reloadButton = useRef(null), retryButton = useRef(null)
  const composing = useRef(false)
  const draftRevision = useRef(0)
  const saving = useRef(false)
  const errorId = useId(), focusRole = useRef(null)
  const focusedError = useRef(null)
  useEffect(() => {
    if (!error) focusedError.current = null
    if (focusRole.current) {
      form.current?.querySelector(`[data-role-layer="${focusRole.current}"]`)?.focus()
      focusRole.current = null
      return
    }
    if (busy) return
    if (error && focusedError.current !== error) {
      focusedError.current = error
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
    setUndo(null)
    const restoreRetryFocus = document.activeElement === retryButton.current && retryButton.current !== null
    const controller = new AbortController(); pending.current = controller
    setBusy(true); setError(null)
    Promise.resolve().then(() => current(controller) ? callbacks.current.loadProfile(customerKey, { signal: controller.signal }) : undefined)
      .then(record => {
        if (!current(controller)) return
        if (!valid(record, customerKey)) throw new Error('invalid scoped record')
        const copy = clone(record.roles)
        if (restoreRetryFocus) focusRole.current = 'module'
        setProfile(record); if (!keepEdits) { setDraft(copy);onDirtyChange?.(false) }
        setStale(false)
        if (reconcile) setMessage('Stored profile reloaded.')
        setReconcile(false)
      }).catch(() => { if (current(controller)) setError('The profile could not be loaded for this customer.') })
      .finally(() => { if (current(controller)) { pending.current = null; setBusy(false) } })
  }
  useEffect(() => {
    alive.current = true; load(false)
    return () => { alive.current = false; pending.current?.abort(); pending.current = null }
  }, [])
  const save = event => {
    event.preventDefault()
    event.stopPropagation()
    if (composing.current || event.nativeEvent?.isComposing) return
    if (!profile || !draft || pending.current || stale) return
    if (!draft.module?.block?.trim() || !draft.module?.layer?.trim()) { setError('Module block and layer are required.'); return }
    if (['roof', 'obstruction'].some(role => draft[role] && !draft[role].layer?.trim())) {
      setError('Each added role needs a layer.'); return
    }
    const controller = new AbortController(); pending.current = controller
    const submittedRevision = draftRevision.current
    saving.current = true
    setBusy(true); setError(null); setMessage(null)
    Promise.resolve().then(() => current(controller) ? callbacks.current.saveProfile(customerKey, clone(draft),
      { expected_version: profile.version, signal: controller.signal }) : undefined)
      .then(record => {
        if (!current(controller)) return
        if (record?.ok === false) throw record
        if (!valid(record, customerKey) || record.profile_id !== profile.profile_id || record.version <= profile.version) throw new Error('invalid saved record')
        const copy = clone(record.roles)
        setProfile(record)
        if (draftRevision.current === submittedRevision) {
          setDraft(copy); setMessage('Profile saved.'); onDirtyChange?.(false)
        } else {
          setMessage('Profile saved. Newer edits are unsaved.'); onDirtyChange?.(true)
        }
      }).catch(reason => {
        if (!current(controller)) return
        const conflict = ['STALE_VERSION', 'VERSION_CONFLICT'].includes(reason?.code)
        setStale(conflict)
        setError(conflict ? 'The profile changed. Your edits are kept. Reload the version to retry.' : 'The profile could not be saved. Your edits are kept.')
      }).finally(() => { if (current(controller)) { pending.current = null; saving.current = false; setBusy(false) } })
  }
  const resetDraft = () => {
    if (!profile) return
    setUndo(null)
    draftRevision.current++
    setDraft(clone(profile.roles)); setError(null); onDirtyChange?.(saving.current)
    setMessage(saving.current ? 'Draft reset. The pending save may still change the stored profile.' : 'Draft reset to the loaded profile.')
  }
  const cancelSave = () => {
    if (!saving.current || !pending.current) return
    pending.current.abort(); pending.current = null; saving.current = false
    setUndo(null)
    draftRevision.current++; setDraft(clone(profile.roles)); onDirtyChange?.(false)
    setBusy(false); setError(null); setReconcile(true)
    setMessage('Save cancelled. Stored changes may have completed. Reload the profile to reconcile.')
  }
  const edit = (role, field, value) => { setUndo(null); draftRevision.current++; onDirtyChange?.(true);setDraft(previous => ({ ...previous, [role]: { ...previous[role], [field]: value } })); setMessage(null) }
  const addRole = role => { setUndo(null); draftRevision.current++; onDirtyChange?.(true);focusRole.current = role; setDraft(previous => ({ ...previous, [role]: { layer: '' } })); setMessage(null); setError(null) }
  const removeRole = role => { setUndo(null); draftRevision.current++; onDirtyChange?.(true);focusRole.current = 'module'; setDraft(previous => { const next = { ...previous }; delete next[role]; return next }); setMessage(null); setError(null) }
  const restoreRole = role => {
    if (!profile || !draft || !roleNames.includes(role)) return
    setUndo({ draft: clone(draft), role })
    const next = { ...draft }
    if (Object.hasOwn(profile.roles, role)) next[role] = clone(profile.roles[role])
    else delete next[role]
    draftRevision.current++
    focusRole.current = next[role] ? role : 'module'
    setDraft(next); setError(null)
    onDirtyChange?.(saving.current || roleNames.some(name => roleDiffers(profile.roles, next, name)))
    setMessage(`Loaded ${role} value restored in draft. Save to keep changes.`)
  }
  const undoRestore = () => {
    if (!undo || !profile) return
    const next = clone(undo.draft)
    draftRevision.current++
    focusRole.current = next[undo.role] ? undo.role : 'module'
    setDraft(next); setUndo(null); setError(null)
    onDirtyChange?.(saving.current || roleNames.some(role => roleDiffers(profile.roles, next, role)))
    setMessage('Last restore undone in draft. Save to keep changes.')
  }
  const changedRoles = profile && draft ? roleNames.filter(role => roleDiffers(profile.roles, draft, role)) : []
  const pin = profile && applicationRecord?.customer_key === customerKey && applicationRecord.profile_id === profile.profile_id
    && Number.isSafeInteger(applicationRecord.version) && applicationRecord.version > 0 ? applicationRecord : null
  return <section aria-label="Customer role profile">
    <h3>Customer role profile</h3><p>Customer: {customerKey}</p>
    {profile && <><p>Profile id: {profile.profile_id}</p><p aria-live="polite" aria-atomic="true">Profile version: {profile.version}</p></>}
    {pin && <p>Project is pinned to version {pin.version}.</p>}
    {profile && pinnedRoles && <X07ProfileRoleDiff beforeRoles={pinnedRoles} afterRoles={profile.roles} label="Pinned versus current roles" />}
    {error && <p id={errorId} role="alert">{error}</p>}{message && <p role="status">{message}</p>}
    {!profile && !busy && <button ref={retryButton} type="button" onClick={() => load(false)}>Retry loading profile</button>}
    {draft && <form ref={form} onSubmit={save} onReset={event => { event.preventDefault(); event.stopPropagation(); resetDraft() }} aria-describedby={error ? errorId : undefined}
      onCompositionStart={() => { composing.current = true }}
      onCompositionEnd={() => { composing.current = false }}
      onKeyDown={event => { if (event.key === 'Enter' && (composing.current || event.nativeEvent.isComposing || event.keyCode === 229)) event.preventDefault() }}>
      <X07ProfileRoleDiff beforeRoles={profile.roles} afterRoles={draft} label={`Draft changes from loaded version ${profile.version}`} />
      {changedRoles.map(role => <button key={role} type="button" onClick={() => restoreRole(role)}>
        Restore loaded {role} value
      </button>)}
      {undo && <button type="button" onClick={undoRestore}>Undo last restore</button>}
      <fieldset><legend>Drawing roles</legend>
        {Object.keys(draft).filter(role => ['module', 'roof', 'obstruction'].includes(role)).map(role => {
          const label = role[0].toUpperCase() + role.slice(1)
          return <div key={role}>
            {(role === 'module' || typeof draft[role].block === 'string') && <label htmlFor={`${errorId}-${role}-block`}>{label} block<input id={`${errorId}-${role}-block`} value={draft[role].block || ''} onChange={event => edit(role, 'block', event.target.value)} /></label>}
            <label htmlFor={`${errorId}-${role}-layer`}>{label} layer<input id={`${errorId}-${role}-layer`} data-role-layer={role} aria-describedby={error ? errorId : undefined} value={draft[role].layer || ''} onChange={event => edit(role, 'layer', event.target.value)} /></label>
            {role !== 'module' && <button type="button" onClick={() => removeRole(role)}>Remove {role} role</button>}
          </div>
        })}
        {['roof', 'obstruction'].filter(role => !draft[role]).map(role =>
          <button key={role} type="button" onClick={() => addRole(role)}>Add {role} role</button>)}
      </fieldset>
      <button ref={saveButton} aria-describedby={error ? errorId : undefined} type="submit" disabled={busy || stale || typeof saveProfile !== 'function'}>Save profile</button>
      <button type="reset">Reset draft</button>
      {busy && saving.current && <button type="button" onClick={cancelSave}>Cancel save</button>}
      {reconcile && <button type="button" disabled={busy} onClick={() => load(false)}>Reload stored profile</button>}
      {stale && <button ref={reloadButton} type="button" disabled={busy} onClick={() => load(true)}>Reload version, keep edits</button>}
    </form>}
    {busy && <p role="status">Reading or saving profile.</p>}
  </section>
}
