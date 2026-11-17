import X07RoleConfirmation from './X07RoleConfirmation.jsx'
import { useDrawingRoleRead } from './x07DrawingRoleRead.js'

export default function X07DrawingRoleReview(props) {
  const read = useDrawingRoleRead(props)
  if (!read.available) return null
  if (read.status === 'loading') return <p role="status">Reading drawing roles.</p>
  if (read.status === 'error') return <div>
    <p role="status">Drawing roles could not be read.</p>
    <button type="button" onClick={read.retry}>Retry reading roles</button>
  </div>
  const confirm = async (selection, options) => {
    if (!read.isCurrent() || options.signal.aborted) return { ok: false, message: 'The drawing scope changed. Read roles again.' }
    return props.onConfirm(selection, { ...options, authorityKey: props.authorityKey,
      solarBinding: { ...props.solarBinding } })
  }
  return <X07RoleConfirmation key={read.readKey} proposal={read.data}
    onConfirm={typeof props.onConfirm === 'function' ? confirm : undefined} onCancel={props.onCancel} />
}
