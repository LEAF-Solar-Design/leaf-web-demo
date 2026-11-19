import { validTrackers } from './x07TrackerRoles.js'
import { useEffect, useMemo, useRef, useState } from 'react'
import X07DrawingRoleProfileReview from './X07DrawingRoleProfileReview.jsx'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'

const current = predicate => {
  try { return typeof predicate === 'function' && predicate() === true } catch { return false }
}
const label = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 512
const validRoles = roles => roles && typeof roles === 'object' && !Array.isArray(roles)
  && label(roles.module?.block) && label(roles.module?.layer)
  && Object.entries(roles).every(([name, role]) => name === 'trackers' ? validTrackers(role) : ['module', 'roof', 'obstruction'].includes(name)
    && label(role?.layer) && (role.block === undefined || label(role.block)))
const profile = (value, customer) => value?.customer_key === customer && label(value.profile_id)
  && Number.isSafeInteger(value.version) && value.version > 0 && validRoles(value.roles)

export default function X07ProjectProfileReview(props) {
  const { solarBinding: b, projectClient: client, authorityKey, isCurrent } = props
  const epoch = useRef({ client, predicate: isCurrent, value: 0 })
  if (epoch.current.client !== client || epoch.current.predicate !== isCurrent) {
    epoch.current = { client, predicate: isCurrent, value: epoch.current.value + 1 }
  }
  if (!b?.organization_id || !b.project_id || !b.drawing_id || !b.input_version_id || !authorityKey
      || typeof client?.readRoleProfile !== 'function' || !current(isCurrent)) return null
  return <ScopedReview key={JSON.stringify([b, authorityKey, epoch.current.value])} {...props} />
}

function ScopedReview(props) {
  const { solarBinding: b, projectClient: client, isCurrent } = props
  const [view, setView] = useState(null), [attempt, setAttempt] = useState(0)
  const callbacks = useMemo(() => {
    const unpack = response => {
      if (!current(isCurrent) || response?.ok !== true) throw new Error('Profile operation unavailable')
      return response.data
    }
    const checkedProfile = response => {
      const value = unpack(response)?.profile
      if (!profile(value, b.organization_id)) throw new Error('Profile operation unavailable')
      return value
    }
    return {
      loadProfile: async (customer, options) => {
        if (customer !== b.organization_id) throw new Error('Profile operation unavailable')
        return checkedProfile(await client.readRoleProfile(options))
      },
      saveProfile: async (customer, roles, options) => {
        if (customer !== b.organization_id) throw new Error('Profile operation unavailable')
        return checkedProfile(await client.saveRoleProfile(roles, options))
      },
      onConfirm: async (selection, options) => unpack(await client.confirmRoleProfile(selection,
        { signal: options.signal, expected_version: view?.expected_version })),
    }
  }, [client, b, isCurrent, view?.expected_version])
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setView(null)
    Promise.resolve().then(() => client.readRoleProfile({ signal: controller.signal })).then(response => {
      if (!active || controller.signal.aborted || !current(isCurrent)) return
      const data = response?.data
      if (response?.ok !== true || data?.customer_key !== b.organization_id || data?.project_key !== b.project_id
          || data?.drawing_key !== b.drawing_id || data?.input_version_id !== b.input_version_id
          || !Number.isSafeInteger(data.expected_version) || data.expected_version < 0) throw new Error('Invalid profile')
      const pin = data.application_record
      if (pin !== null && (!profile(pin, b.organization_id) || pin.project_key !== b.project_id)) throw new Error('Invalid pin')
      setView(data)
    }).catch(() => {
      if (active && !controller.signal.aborted && current(isCurrent)) setView({ error: true })
    })
    return () => { active = false; controller.abort() }
  }, [client, b, isCurrent, attempt])
  if (!view) return <p role="status">Reading customer role profile.</p>
  if (view.error) return <div><p role="status">Customer role profile could not be read.</p>
    <button type="button" onClick={() => setAttempt(value => value + 1)}>Retry customer profile</button></div>
  if (view.application_record) return <section aria-label="Confirmed drawing profile">
    <p role="status">Saved drawing roles loaded.</p>
    <X07CustomerProfileEditor customerKey={b.organization_id} drawingKey={b.drawing_id}
      applicationRecord={view.application_record} pinnedRoles={view.application_record.roles} {...callbacks} />
  </section>
  return <X07DrawingRoleProfileReview {...props} customerKey={b.organization_id} drawingKey={b.drawing_id} {...callbacks} />
}
