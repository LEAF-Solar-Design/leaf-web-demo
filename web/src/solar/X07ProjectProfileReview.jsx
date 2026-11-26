import { Fragment, useEffect, useId, useMemo, useRef, useState } from 'react'
import { previewRoofPlacementChanges } from '../api.js'
import X07DrawingRoleProfileReview from './X07DrawingRoleProfileReview.jsx'
import X07CustomerProfileEditor from './X07CustomerProfileEditor.jsx'
import X07ProfileHistory from './X07ProfileHistory.jsx'
import X07ProfilePortfolio from './X07ProfilePortfolio.jsx'
import { validTrackers } from './x07TrackerRoles.js'

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

function UndoCopyReview({ binding, isCurrent, choices, before, after }) {
  const selectId = useId()
  const [handle, setHandle] = useState(choices[0].handle)
  const [retry, setRetry] = useState(0)
  const [report, setReport] = useState(null)
  const result = report?.handle === handle && report.retry === retry ? report : null

  useEffect(() => {
    if (!current(isCurrent)) return
    const controller = new AbortController()
    const version = binding.input_version_id
    const payload = { previous_input_version_id: version, previous_role: before,
      block: after.block, layer: after.layer, windows: [{ within: { outline_handle: handle } }] }
    Promise.resolve().then(() => {
      if (!controller.signal.aborted) {
        return previewRoofPlacementChanges(binding.organization_id, binding, payload, { signal: controller.signal })
      }
    })
      .then(data => {
        if (controller.signal.aborted || !current(isCurrent)) return
        const paths = data?.windows?.[0]?.added_paths
        if (data?.input_version_id !== version || data?.previous_input_version_id !== version
            || !Array.isArray(data?.windows) || data.windows.length !== 1
            || !Array.isArray(paths) || paths.length > 256
            || !paths.every(path => typeof path === 'string' && path.length > 0)
            || new Set(paths).size !== paths.length) throw new Error('Invalid placement report')
        setReport({ handle, retry, paths })
      }).catch(() => {
        if (!controller.signal.aborted && current(isCurrent)) setReport({ handle, retry, error: true })
      })
    return () => controller.abort()
  }, [binding, isCurrent, before, after, handle, retry])

  return <section aria-label="Panels returned after Undo">
    <label htmlFor={selectId}>Design copy outline</label>
    <select id={selectId} value={handle} onChange={event => {
      setReport(null)
      setHandle(event.target.value)
    }}>
      {choices.map(choice => <option key={choice.handle} value={choice.handle}>
        {choice.layer ? `${choice.layer} · ${choice.handle}` : choice.handle}
      </option>)}
    </select>
    {!result && <p role="status">Checking returned panels.</p>}
    {result?.error && <><p role="alert">Returned panels could not be checked for this copy.</p>
      <button type="button" onClick={() => setRetry(value => value + 1)}>Retry panel preview</button></>}
    {result?.paths?.length === 0 && <p role="status">No panels returned in this copy.</p>}
    {!!result?.paths?.length && <ul aria-label="Returned panel placements">
      {result.paths.map(path => <li key={path}>{path}</li>)}
    </ul>}
  </section>
}

export default function X07ProjectProfileReview(props) {
  const { solarBinding: b, projectClient: client, authorityKey, isCurrent } = props
  const epoch = useRef({ client, predicate: isCurrent, value: 0 })
  const [profileRefresh, setProfileRefresh] = useState(0)
  if (epoch.current.client !== client || epoch.current.predicate !== isCurrent) {
    epoch.current = { client, predicate: isCurrent, value: epoch.current.value + 1 }
  }
  if (!b?.organization_id || !b.project_id || !b.drawing_id || !b.input_version_id || !authorityKey
      || typeof client?.readRoleProfile !== 'function' || !current(isCurrent)) return null
  const key = JSON.stringify([b, authorityKey, epoch.current.value])
  return <Fragment key={key}>
    <ScopedReview key={profileRefresh} {...props} />
    <X07ProfilePortfolio binding={b} client={client} isCurrent={() => current(isCurrent)}
      onConfirmed={() => setProfileRefresh(value => value + 1)} />
  </Fragment>
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
  const history = typeof client.readRoleProfileHistory === 'function' && <X07ProfileHistory
    client={client} isCurrent={() => current(isCurrent)} />
  const outlines = Array.isArray(view.outline_choices) ? view.outline_choices.filter(choice =>
    typeof choice?.handle === 'string' && /^[0-9A-F]{1,32}$/.test(choice.handle)
      && typeof choice.layer === 'string') : []
  const renderUndoReview = outlines.length ? (before, after) => (
    [before, after].every(role => label(role?.block) && label(role?.layer))
      ? <UndoCopyReview binding={b} isCurrent={isCurrent} choices={outlines} before={before} after={after} /> : null
  ) : undefined
  const roofCandidates = Array.isArray(view.roof_inheritance_candidates)
    ? view.roof_inheritance_candidates.filter(row =>
      typeof row?.handle === 'string' && /^[0-9A-F]{1,32}$/.test(row.handle)
      && label(row.role?.layer) && (row.kind === 'outline' ? row.role.block === undefined
        : row.kind === 'insert' && label(row.role.block))
      && Array.isArray(row.bounds) && row.bounds.length === 4 && row.bounds.every(Number.isFinite))
    : []
  if (view.application_record) return <section aria-label="Confirmed drawing profile">
    <p role="status">Saved drawing roles loaded.</p>
    <X07CustomerProfileEditor customerKey={b.organization_id} drawingKey={b.drawing_id}
      applicationRecord={view.application_record} pinnedRoles={view.application_record.roles}
      renderUndoReview={renderUndoReview} roofPreview={{ profile: view.profile, candidates: roofCandidates }}
      {...callbacks} />
    {history}
  </section>
  return <>
    <X07DrawingRoleProfileReview {...props} customerKey={b.organization_id} drawingKey={b.drawing_id}
      outlineChoices={view.outline_choices} {...callbacks} />
    {view.profile && history}
  </>
}
