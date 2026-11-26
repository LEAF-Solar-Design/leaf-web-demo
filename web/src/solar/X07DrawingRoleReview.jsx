import { useRef, useState } from 'react'
import X07RoleConfirmation from './X07RoleConfirmation.jsx'
import { useDrawingRoleRead } from './x07DrawingRoleRead.js'

export const isSuccessfulRoleConfirmation = result => result !== null && typeof result === 'object'
  && !Array.isArray(result) && result.ok === true

export default function X07DrawingRoleReview(props) {
  const read = useDrawingRoleRead(props)
  const [cancelled, setCancelled] = useState(null)
  const submitted = useRef(null)
  if (!read.available) return null
  const cancelState = cancelled?.key === read.readKey && cancelled.client === props.projectClient ? cancelled.kind : null
  if (cancelState) return <div>
    <p role="status">Role confirmation cancelled.</p>
    {cancelState === 'before-submit' ? <button type="button" onClick={() => {
      read.retry(); setCancelled(null)
    }}>Read roles again</button> : <p>Saved changes may have completed. Inspect the project before confirming again.</p>}
  </div>
  if (read.status === 'loading') return <p role="status">Reading drawing roles.</p>
  if (read.status === 'error') return <div>
    <p role="status">Drawing roles could not be read.</p>
    <button type="button" onClick={read.retry}>Retry reading roles</button>
  </div>
  const confirm = async (selection, options) => {
    if (!read.isCurrent() || options.signal.aborted) return { ok: false, message: 'The drawing scope changed. Read roles again.' }
    submitted.current = { key: read.readKey, client: props.projectClient }
    try {
      const result = await props.onConfirm(selection, { ...options, authorityKey: props.authorityKey,
        solarBinding: { ...props.solarBinding } })
      if (isSuccessfulRoleConfirmation(result)) return { ok: true }
    } catch { /* Callback details stay in the owning host. */ }
    return { ok: false, message: 'Roles could not be saved. Try again.' }
  }
  return <X07RoleConfirmation key={read.readKey} proposal={read.data} outlineChoices={props.outlineChoices}
    onConfirm={typeof props.onConfirm === 'function' ? confirm : undefined} onCancel={() => {
      const started = submitted.current?.key === read.readKey && submitted.current.client === props.projectClient
      setCancelled({ key: read.readKey, client: props.projectClient, kind: started ? 'after-submit' : 'before-submit' })
      props.onCancel?.()
    }} />
}
