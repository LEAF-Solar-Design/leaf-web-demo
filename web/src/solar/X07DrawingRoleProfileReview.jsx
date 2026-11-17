import { useEffect, useId, useRef, useState } from 'react'
import X07DrawingRoleReview, { isSuccessfulRoleConfirmation } from './X07DrawingRoleReview.jsx'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'

const current = predicate => {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}
const object = value => value && typeof value === 'object' && !Array.isArray(value)
const label = value => typeof value === 'string' && value.trim() && value.length <= 512
const validRoles = roles => object(roles) && object(roles.module)
  && label(roles.module.block) && label(roles.module.layer)
  && Object.entries(roles).every(([name, role]) => ['module', 'roof', 'obstruction'].includes(name)
    && object(role) && label(role.layer) && (role.block === undefined || label(role.block)))

function confirmedPin(result, owner) {
  const pin = result.applied_profile
  // Project pins are project-scoped in the store. The workflow result supplies
  // drawing identity; do not infer it from the customer or invent a store field.
  const drawing = result.drawing_key ?? pin?.drawing_key
  if (!object(pin) || pin.customer_key !== owner.customerKey
      || !label(owner.solarBinding?.project_id) || pin.project_key !== owner.solarBinding.project_id
      || !label(owner.drawingKey) || drawing !== owner.drawingKey
      || (pin.drawing_key !== undefined && pin.drawing_key !== owner.drawingKey)
      || (owner.solarBinding.drawing_id !== undefined && owner.solarBinding.drawing_id !== owner.drawingKey)
      || !label(pin.profile_id) || !Number.isSafeInteger(pin.version) || pin.version < 1
      || !validRoles(pin.roles)) return null
  const roles = Object.fromEntries(Object.entries(pin.roles).map(([name, role]) => [name,
    Object.freeze({ ...(role.block === undefined ? {} : { block: role.block }), layer: role.layer })]))
  return Object.freeze({ customer_key: pin.customer_key, project_key: pin.project_key,
    drawing_key: drawing, profile_id: pin.profile_id, version: pin.version, roles: Object.freeze(roles) })
}

export default function X07DrawingRoleProfileReview(props) {
  const ownership = useRef({ client: props.projectClient, predicate: props.isCurrent, epoch: 0 })
  if (ownership.current.client !== props.projectClient || ownership.current.predicate !== props.isCurrent) {
    ownership.current = { client: props.projectClient, predicate: props.isCurrent, epoch: ownership.current.epoch + 1 }
  }
  if (typeof props.customerKey !== 'string' || !props.customerKey.trim()) return <p role="alert">Choose a customer to review drawing roles.</p>
  if (!current(props.isCurrent)) return null
  return <ScopedProfileReview key={JSON.stringify([props.customerKey, props.authorityKey, props.solarBinding,
    props.drawingKey, ownership.current.epoch])} {...props} />
}

function ScopedProfileReview(props) {
  const [applied, setApplied] = useState(null)
  const container = useRef(null), statusId = useId()
  const alive = useRef(true), latest = useRef(props)
  latest.current = props
  useEffect(() => {
    alive.current = true
    return () => { alive.current = false }
  }, [])
  useEffect(() => {
    if (!applied) return
    let focused = false
    const focusEditor = () => {
      if (focused || !alive.current) return
      const editor = container.current?.querySelector('section[aria-label="Customer role profile"]')
      const target = editor?.querySelector('input[data-role-layer="module"], button')
      if (target) { target.focus(); focused = true }
    }
    const observer = new MutationObserver(focusEditor)
    if (container.current) observer.observe(container.current, { childList: true, subtree: true })
    focusEditor()
    return () => observer.disconnect()
  }, [applied])
  const confirm = async (selection, options) => {
    const owner = props
    const result = await owner.onConfirm(selection, options)
    if (!alive.current || options.signal.aborted || !current(owner.isCurrent)
        || latest.current.projectClient !== owner.projectClient || latest.current.isCurrent !== owner.isCurrent
        || !isSuccessfulRoleConfirmation(result)) return { ok: false }
    const pin = confirmedPin(result, owner)
    if (!pin) return { ok: false }
    setApplied({ client: owner.projectClient, pin })
    return { ok: true }
  }
  if (applied?.client === props.projectClient) return <section ref={container} aria-label="Confirmed drawing profile" aria-describedby={statusId}>
    <p id={statusId} role="status">Roles confirmed.</p>
    <section aria-label="Pinned drawing roles"><h3>Pinned drawing roles</h3>
      <p>Pinned profile version: {applied.pin.version}</p>
      <dl>{Object.entries(applied.pin.roles).map(([name, role]) => <div key={name}>
        <dt>{name[0].toUpperCase() + name.slice(1)}</dt><dd>{role.block ? `${role.block} on ` : ''}{role.layer}</dd>
      </div>)}</dl>
    </section>
    <X07CustomerProfileEditor customerKey={props.customerKey} drawingKey={props.drawingKey}
      loadProfile={props.loadProfile} saveProfile={props.saveProfile} applicationRecord={applied.pin} pinnedRoles={applied.pin.roles} />
  </section>
  return <X07DrawingRoleReview {...props} onConfirm={typeof props.onConfirm === 'function' ? confirm : undefined} />
}
