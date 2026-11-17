import { useEffect, useRef, useState } from 'react'
import X07DrawingRoleReview, { isSuccessfulRoleConfirmation } from './X07DrawingRoleReview.jsx'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'

const current = predicate => {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}

export default function X07DrawingRoleProfileReview(props) {
  if (typeof props.customerKey !== 'string' || !props.customerKey.trim()) return <p role="alert">Choose a customer to review drawing roles.</p>
  if (!current(props.isCurrent)) return null
  return <ScopedProfileReview key={JSON.stringify([props.customerKey, props.authorityKey, props.solarBinding])} {...props} />
}

function ScopedProfileReview(props) {
  const [applied, setApplied] = useState(null)
  const alive = useRef(true), latest = useRef(props)
  latest.current = props
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  const confirm = async (selection, options) => {
    const owner = props
    const result = await owner.onConfirm(selection, options)
    if (!alive.current || options.signal.aborted || !current(owner.isCurrent)
        || latest.current.projectClient !== owner.projectClient || latest.current.isCurrent !== owner.isCurrent
        || !isSuccessfulRoleConfirmation(result)) return { ok: false }
    const pin = result.applied_profile ?? owner.applicationRecord
    if (pin && pin.customer_key !== owner.customerKey) return { ok: false }
    setApplied({ client: owner.projectClient, pin })
    return { ok: true }
  }
  if (applied?.client === props.projectClient) return <>
    <p role="status">Roles confirmed.</p>
    <X07CustomerProfileEditor customerKey={props.customerKey} drawingKey={props.drawingKey}
      loadProfile={props.loadProfile} saveProfile={props.saveProfile} applicationRecord={applied.pin} />
  </>
  return <X07DrawingRoleReview {...props} onConfirm={typeof props.onConfirm === 'function' ? confirm : undefined} />
}
