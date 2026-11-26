import { useEffect, useId, useRef, useState } from 'react'
import { exportRemovedProfileRoles, listProjects, openProject, previewPanelGroupChanges, previewRackPlacementOverrides,
  previewRoofPlacementChanges, reviewRoleProfileReuse } from '../api.js'

const limit = 16
const uuid = value => typeof value === 'string' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value)
const tuple = row => [row.project_id, row.drawing_id, row.input_version_id]
const profileVersion = value => Number.isSafeInteger(value) && value > 0
const statuses = new Map([
  ['compatible', 'Can reuse saved roles'],
  ['needs_choice', 'Needs a new role choice'],
  ['unavailable', 'Drawing data unavailable'],
])
const reasons = new Map([
  ['NO_PROFILE', 'This customer has no saved profile.'],
  ['ROLE_NOT_FOUND', 'A saved block or layer is missing from this revision.'],
  ['AMBIGUOUS_ROLE_LABEL', 'More than one drawing label matches a saved role.'],
  ['AMBIGUOUS_ALIAS', 'More than one block matches a saved role.'],
  ['ANONYMOUS_ALIAS_REQUIRED', 'A saved anonymous block needs a drawing-specific match.'],
  ['DRAWING_UNREADABLE', 'Drawing roles could not be read.'],
])
const panelUnavailable = new Map([
  ['CONFIRMED_WINDOW_UNAVAILABLE', 'This drawing version has no recorded confirmation window.'],
  ['PROFILE_REQUIRES_REVIEW', 'A saved role needs review before panel selection can be compared.'],
  ['PLACEMENT_IDENTITY_UNAVAILABLE', 'The drawing did not provide a unique identity for every selected panel.'],
])
const exportStatuses = new Set(['uses_removed_roles', 'no_removed_roles', 'unpinned', 'outside_snapshot', 'unavailable'])

function validPanelComparison(comparison, olderPin) {
  if (!olderPin) return comparison === null
  if (comparison?.status === 'unavailable') return typeof comparison.reason === 'string' && !!comparison.reason
  if (!comparison || ![comparison.added, comparison.removed].every(n => Number.isSafeInteger(n) && n >= 0)) {
    return false
  }
  return comparison.status === (comparison.added || comparison.removed ? 'different' : 'same')
}

function checkedResults(data, organizationId, selected) {
  if (data?.customer_key !== organizationId || !Array.isArray(data.rows) || data.rows.length !== selected.length
      || (data.current_profile_version !== null && !profileVersion(data.current_profile_version))) {
    throw new Error('Invalid reuse review')
  }
  const current = data.current_profile_version
  return selected.map((item, index) => {
    const row = data.rows[index]
    if (!row || tuple(row).some((value, offset) => value !== tuple(item.revision)[offset])
        || !statuses.has(row.status) || typeof row.pinned !== 'boolean'
        || (row.status === 'compatible' ? row.reason !== null : typeof row.reason !== 'string' || !row.reason)
        || (row.profile_id === null
          ? current !== null || row.profile_version !== null || row.pinned || row.status === 'compatible'
          : typeof row.profile_id !== 'string' || !row.profile_id || !profileVersion(row.profile_version)
            || current === null || (row.pinned ? row.profile_version > current : row.profile_version !== current))) {
      throw new Error('Invalid reuse decision')
    }
    const olderPin = row.pinned && row.profile_version < current
    const comparison = row.role_comparison
    if (olderPin
      ? !Array.isArray(comparison) || comparison.some(choice => !choice || typeof choice.role !== 'string'
        || (choice.field !== null && typeof choice.field !== 'string')
        || !Object.hasOwn(choice, 'pinned') || !Object.hasOwn(choice, 'current'))
      : comparison !== null) {
      throw new Error('Invalid saved role comparison')
    }
    if (!validPanelComparison(row.panel_comparison, olderPin)) throw new Error('Invalid panel comparison')
    return { ...row, label: item.label, current }
  })
}

function downloadRemovedRoles(data, organizationId, snapshot, selected) {
  if (data?.customer_key !== organizationId || data.profile_id !== snapshot.profile_id
      || data.through_version !== snapshot.through_version || !Array.isArray(data.rows)
      || data.rows.length !== selected.length) throw new Error('Invalid removed role export')
  const rows = data.rows.map((row, index) => {
    const item = selected[index]
    if (!row || tuple(row).some((value, offset) => value !== tuple(item.revision)[offset])
        || !exportStatuses.has(row.status)
        || (row.pinned_profile_version !== null && !profileVersion(row.pinned_profile_version))
        || (row.status === 'unavailable' ? typeof row.reason !== 'string' || !row.reason : row.reason !== null)
        || !Array.isArray(row.removed_roles)
        || (row.status === 'uses_removed_roles') !== (row.removed_roles.length > 0)
        || row.removed_roles.some(role => !role || typeof role.role !== 'string' || !role.role
          || !role.applied || typeof role.applied !== 'object')) {
      throw new Error('Invalid removed role decision')
    }
    return { ...row, label: item.label }
  })
  const content = JSON.stringify({ ...data, rows }, null, 2)
  const url = URL.createObjectURL(new Blob([content], { type: 'application/json' }))
  try {
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = 'removed-profile-roles.json'
    anchor.click()
  } finally {
    URL.revokeObjectURL(url)
  }
}

const validCount = count => Number.isSafeInteger(count) && count >= 0
const validPath = path => typeof path === 'string' && !!path
const validHandle = handle => /^[0-9A-F]{1,32}$/.test(handle)

function checkedRoofChanges(data, previous, current, roofs) {
  if (data?.previous_input_version_id !== previous || data.input_version_id !== current
      || !Array.isArray(data.windows) || data.windows.length !== roofs.length) {
    throw new Error('Invalid revision comparison')
  }
  return data.windows.map((row, index) => {
    if (!row || ![row.previous_count, row.current_count, row.retained_count].every(validCount)
        || !Array.isArray(row.added_paths) || !Array.isArray(row.removed_paths)) {
      throw new Error('Invalid roof comparison')
    }
    const paths = [...row.added_paths, ...row.removed_paths]
    if (paths.length > 256 || !paths.every(validPath) || new Set(paths).size !== paths.length
        || row.previous_count - row.retained_count !== row.removed_paths.length
        || row.current_count - row.retained_count !== row.added_paths.length
        || row.count_delta !== row.current_count - row.previous_count) {
      throw new Error('Inconsistent roof comparison')
    }
    return { ...row, roof: roofs[index] }
  })
}

function RoofPlacementComparison({ organizationId, pair, run }) {
  const [block, setBlock] = useState('')
  const [layer, setLayer] = useState('')
  const [handles, setHandles] = useState('')
  const [results, setResults] = useState(null)
  const [previous, current] = pair
  const roofs = handles.trim().toUpperCase().split(/[,\s]+/).filter(Boolean)
  const canCompare = block.trim() && layer.trim() && roofs.length > 0 && roofs.length <= 4
    && roofs.every(validHandle) && new Set(roofs).size === roofs.length

  function compare() {
    if (!canCompare) return
    setResults(null)
    const previousId = previous.revision.input_version_id
    const payload = {
      previous_input_version_id: previousId, block: block.trim(), layer: layer.trim(),
      windows: roofs.map(outline_handle => ({ within: { outline_handle } })),
    }
    run(signal => previewRoofPlacementChanges(organizationId, current.revision, payload, { signal }),
      data => setResults(checkedRoofChanges(data, previousId, current.revision.input_version_id, roofs)),
      'Roof panel changes could not be compared. Check that the same outlines exist in both versions.')
  }

  return <fieldset onChange={() => setResults(null)}>
    <legend>Roof panel changes</legend>
    <p>{previous.label} to {current.label}</p>
    <label>Module block <input value={block} maxLength={512} onChange={event => setBlock(event.target.value)} /></label>
    <label>Module layer <input value={layer} maxLength={512} onChange={event => setLayer(event.target.value)} /></label>
    <label>Roof outline handles <input value={handles} maxLength={160} onChange={event => setHandles(event.target.value)} /></label>
    <p>Enter up to four distinct DXF handles separated by spaces or commas.</p>
    <button type="button" disabled={!canCompare} onClick={compare}>Compare roof panels</button>
    {results && <table>
      <caption>Roof panel changes by outline</caption>
      <thead><tr>{['Roof outline', 'Previous', 'Current', 'Change', 'Retained', 'Added paths', 'Removed paths']
        .map(column => <th key={column} scope="col">{column}</th>)}</tr></thead>
      <tbody>{results.map(row => <tr key={row.roof}>
        <th scope="row">{row.roof}</th>
        <td>{row.previous_count}</td><td>{row.current_count}</td>
        <td>{row.count_delta > 0 ? '+' : ''}{row.count_delta}</td><td>{row.retained_count}</td>
        {[row.added_paths, row.removed_paths].map((paths, index) => <td key={index}>
          {paths.length ? <ul>{paths.map(path => <li key={path}>{path}</li>)}</ul> : 'None'}
        </td>)}
      </tr>)}</tbody>
    </table>}
  </fieldset>
}

function checkedGroupChanges(data, previous, current) {
  const validSide = side => side === null || (Array.isArray(side?.dimensions)
    && side.dimensions.length === 2 && side.dimensions.every(n => Number.isFinite(n) && n > 0)
    && (side.group === null || (typeof side.group?.name === 'string' && side.group.name
      && validCount(side.group.panel_count) && side.group.panel_count > 0)))
  const rows = data?.changes
  if (data?.previous_input_version_id !== previous || data.input_version_id !== current
      || typeof data.drawing_units !== 'string' || !data.drawing_units
      || ![data.previous_panel_count, data.current_panel_count].every(validCount)
      || !Array.isArray(rows) || rows.length > 256
      || rows.some(row => typeof row?.handle !== 'string' || !validHandle(row.handle)
        || !validSide(row.previous) || !validSide(row.current) || (!row.previous && !row.current)
        || (row.previous && row.current
          ? typeof row.membership_changed !== 'boolean' || typeof row.size_changed !== 'boolean'
            || (!row.membership_changed && !row.size_changed)
          : row.membership_changed !== null || row.size_changed !== null))
      || new Set(rows.map(row => row.handle)).size !== rows.length) {
    throw new Error('Invalid panel group comparison')
  }
  return data
}

function PanelGroupComparison({ organizationId, pair, run }) {
  const [report, setReport] = useState(null)
  const [previous, current] = pair
  function compare() {
    setReport(null)
    const previousId = previous.revision.input_version_id
    run(signal => previewPanelGroupChanges(organizationId, current.revision, previousId, { signal }),
      data => setReport(checkedGroupChanges(data, previousId, current.revision.input_version_id)),
      'Panel groups could not be compared. Both versions need saved geometry-only groups with matching drawing units.')
  }

  function group(side) {
    if (side === null) return 'Absent'
    return side.group ? side.group.name + ' (' + side.group.panel_count + (side.group.panel_count === 1 ? ' panel)' : ' panels)') : 'Ungrouped'
  }

  function dimensions(side) {
    return side === null ? 'Absent' : side.dimensions.join(' × ')
  }

  return <fieldset>
    <legend>Measured panel groups</legend>
    <button type="button" onClick={compare}>Compare panel groups</button>
    {report && <p role="status">{report.previous_panel_count} previous panels, {report.current_panel_count} current panels.
      {' '}{report.changes.length === 0 ? 'No membership or size changes.' : 'Matching DXF handles identify the comparison rows.'}</p>}
    {report?.changes.length > 0 && <table>
      <caption>Panel group and size changes ({report.drawing_units})</caption>
      <thead><tr>{['DXF handle', 'Previous group', 'Current group', 'Membership',
        'Previous size', 'Current size', 'Measured size'].map(column => <th scope="col" key={column}>{column}</th>)}</tr></thead>
      <tbody>{report.changes.map(row => <tr key={row.handle}>
        <th scope="row">{row.handle}</th><td>{group(row.previous)}</td><td>{group(row.current)}</td>
        <td>{row.previous === null ? 'Added' : row.current === null ? 'Removed'
          : row.membership_changed ? 'Changed' : 'Retained'}</td>
        <td>{dimensions(row.previous)}</td><td>{dimensions(row.current)}</td>
        <td>{row.size_changed === null ? 'Not applicable' : row.size_changed ? 'Changed' : 'Retained'}</td>
      </tr>)}</tbody>
    </table>}
  </fieldset>
}

const blankRack = () => ({ handle: '', paths: '' })
const rackStatus = { unchanged: 'Still matches', moved: 'Moved and still matches', missing: 'Missing' }
const pathPart = /^[0-9A-F]{1,32}\[(0|[1-9][0-9]{0,5}),(0|[1-9][0-9]{0,5})\]$/
function validRackPath(path) {
  const parts = path.split('/')
  return parts.length <= 64 && parts.every(part => pathPart.test(part))
}
const validOrigin = value => Array.isArray(value) && value.length === 2 && value.every(Number.isFinite)
const sameOrigin = (first, second) => first.every((coordinate, index) => coordinate === second[index])

function checkedRackOverrides(data, previousId, currentId, drafts) {
  if (data?.previous_input_version_id !== previousId || data.input_version_id !== currentId
      || !Array.isArray(data.overrides) || data.overrides.length !== drafts.length) {
    throw new Error('Invalid override preview')
  }
  return drafts.map((draft, index) => {
    const row = data.overrides[index]
    const placements = row?.placements
    const window = row?.selection?.design_window
    if (window?.within?.outline_handle !== draft.within.outline_handle
        || (row.boundary_changed !== undefined && typeof row.boundary_changed !== 'boolean')
        || !Array.isArray(window.placement_paths) || window.placement_paths.length !== draft.placement_paths.length
        || window.placement_paths.some((path, i) => path !== draft.placement_paths[i])
        || !Array.isArray(placements) || placements.length !== draft.placement_paths.length
        || new Set(placements.map(item => item?.path)).size !== placements.length
        || placements.some(item => !draft.placement_paths.includes(item?.path)
          || !Object.hasOwn(rackStatus, item.status) || !validOrigin(item.previous_origin)
          || (item.status === 'missing' ? item.current_origin !== null : !validOrigin(item.current_origin))
          || (item.current_drawing_origin != null && (!validOrigin(item.current_drawing_origin)
            || (item.current_origin && !sameOrigin(item.current_origin, item.current_drawing_origin)))))) {
      throw new Error('Invalid preview placements')
    }
    const paths = placements.filter(item => item.status !== 'missing').map(item => item.path).sort()
    return { placements, boundaryChanged: row.boundary_changed,
      window: { within: draft.within, placement_paths: paths } }
  })
}

function exactBinding(data, binding) {
  return data?.customer_key === binding.organization_id && data.project_key === binding.project_id
    && data.drawing_key === binding.drawing_id && data.input_version_id === binding.input_version_id
}

function RackPlacementReview({ binding, client, pair, run, onConfirmed }) {
  const [block, setBlock] = useState('')
  const [layer, setLayer] = useState('')
  const [drafts, setDrafts] = useState(() => [blankRack()])
  const [preview, setPreview] = useState(null)
  const [confirmed, setConfirmed] = useState(null)
  const [previous, current] = pair || []
  const previousId = previous?.revision.input_version_id
  const currentId = current?.revision.input_version_id
  const canWrite = current && tuple(current.revision).every((value, index) => value === tuple(binding)[index])
    && typeof client.confirmRoleProfile === 'function'
  const shown = preview?.previousId === previousId && preview?.currentId === currentId ? preview : null
  const overrides = drafts.map(({ handle, paths }) => ({
    within: { outline_handle: handle.trim().toUpperCase() },
    placement_paths: paths.toUpperCase().split(/\s+/).filter(Boolean).sort(),
  }))
  const ready = pair && block.trim() && layer.trim() && overrides.every(({ within, placement_paths: paths }) =>
    validHandle(within.outline_handle) && paths.length > 0 && paths.length <= 256
    && paths.every(validRackPath) && new Set(paths).size === paths.length)

  function update(index, field, value) {
    setDrafts(rows => rows.map((row, i) => i === index ? { ...row, [field]: value } : row))
  }
  function compare() {
    if (!ready) return
    setPreview(null)
    setConfirmed(null)
    const role = { block: block.trim(), layer: layer.trim() }
    const payload = { ...role, previous_input_version_id: previousId, overrides }
    run(signal => Promise.all([
      previewRackPlacementOverrides(binding.organization_id, current.revision, payload, { signal }),
      canWrite ? client.readRoleProfile({ signal }) : null,
    ]), ([data, profile]) => {
      const view = profile?.data
      const writable = profile?.ok === true && exactBinding(view, binding)
        && validCount(view.expected_version) && view.application_record === null
      setPreview({ previousId, currentId, role, expectedVersion: writable ? view.expected_version : null,
        rows: checkedRackOverrides(data, previousId, currentId, overrides) })
    }, 'Rack overrides could not be compared. Check the original outline and placement paths.')
  }
  function confirm(window, index) {
    if (!canWrite || !shown || shown.expectedVersion === null || !window.placement_paths.length) return
    run(signal => client.confirmRoleProfile({ ...shown.role, design_window: window },
      { signal, expected_version: shown.expectedVersion }), result => {
      const data = result?.data
      const saved = data?.selection?.design_window
      if (result?.ok !== true || !exactBinding(data, binding)
          || saved?.within?.outline_handle !== window.within.outline_handle
          || !Array.isArray(saved.placement_paths) || saved.placement_paths.length !== window.placement_paths.length
          || saved.placement_paths.some((path, i) => path !== window.placement_paths[i])) {
        throw new Error('Invalid override confirmation')
      }
      setConfirmed({ preview: shown, index, count: window.placement_paths.length })
      onConfirmed()
    }, 'The rack override could not be confirmed. Preview it again and check the drawing profile.')
  }
  if (!pair) return null
  return <fieldset onChange={() => setPreview(null)}>
    <legend>Rack override review</legend>
    <label>Override module block <input value={block} maxLength={512} onChange={event => setBlock(event.target.value)} /></label>
    <label>Override module layer <input value={layer} maxLength={512} onChange={event => setLayer(event.target.value)} /></label>
    <p>Paste paths from the earlier drawing, one per line. You can compare up to four rack overrides.</p>
    {drafts.map((draft, index) => <fieldset key={index}>
      <legend>Rack override {index + 1}</legend>
      <label>Roof outline handle <input value={draft.handle} maxLength={32}
        onChange={event => update(index, 'handle', event.target.value)} /></label>
      <label>Earlier placement paths <textarea value={draft.paths}
        maxLength={256 * (64 * 47 + 1)}
        onChange={event => update(index, 'paths', event.target.value)} /></label>
    </fieldset>)}
    <button type="button" disabled={drafts.length === 4} onClick={() => {
      setDrafts(rows => [...rows, blankRack()])
      setPreview(null)
    }}>Add rack override</button>
    {drafts.length > 1 && <button type="button" onClick={() => {
      setDrafts(rows => rows.slice(0, -1))
      setPreview(null)
    }}>Remove last rack override</button>}
    <button type="button" disabled={!ready} onClick={compare}>Preview rack overrides</button>
    {shown && !canWrite && <p>Open the newer drawing version in the workspace to confirm.</p>}
    {shown && canWrite && shown.expectedVersion === null && <p>Confirmation is unavailable for the current drawing profile.</p>}
    {shown?.rows.map(({ placements, boundaryChanged, window }, index) => <section key={index}>
      <table>
        <caption>Rack override {index + 1} review</caption>
        <thead><tr>{['Placement path', 'Status', 'Earlier origin', 'Current drawing origin', 'Panel origin change'].map(column =>
          <th key={column} scope="col">{column}</th>)}</tr></thead>
        <tbody>{placements.map(item => {
          const origin = item.current_drawing_origin ?? item.current_origin
          const status = item.status === 'missing' && origin ? 'Outside revised outline' : rackStatus[item.status]
          return <tr key={item.path}>
            <th scope="row">{item.path}</th><td>{status}</td>
            <td>{item.previous_origin.join(', ')}</td><td>{origin?.join(', ') || 'Unavailable'}</td>
            <td>{origin ? (sameOrigin(item.previous_origin, origin) ? 'Unchanged' : 'Moved') : 'Unavailable'}</td>
          </tr>
        })}</tbody>
      </table>
      {boundaryChanged !== undefined && <p>Roof outline {boundaryChanged ? 'changed' : 'unchanged'}.</p>}
      {canWrite && shown.expectedVersion !== null && confirmed?.preview !== shown && <button type="button"
        disabled={!window.placement_paths.length} onClick={() => confirm(window, index)}>
        Confirm {window.placement_paths.length} matching placements for rack override {index + 1}
      </button>}
      {confirmed?.preview === shown && confirmed.index === index && <p role="status">
        Confirmed {confirmed.count} placements for rack override {index + 1}.
      </p>}
    </section>)}
  </fieldset>
}

function roleValue(value) {
  if (value === null) return '?'
  return typeof value === 'string' ? value : JSON.stringify(value)
}

function RoleComparison({ row }) {
  const panels = row.panel_comparison
  return <details>
    <summary>Compare with current version {row.current}</summary>
    {panels.status === 'unavailable'
      ? <p>{panelUnavailable.get(panels.reason) || 'The exact drawing version is unavailable for comparison.'}</p>
      : <p>{panels.status === 'same' ? 'Same panels in the confirmed window.'
        : `Different panels in the confirmed window. Added ${panels.added}, removed ${panels.removed}.`}</p>}
    <table>
      <caption>Saved role choices</caption>
      <thead><tr>
        <th scope="col">Role or field</th>
        <th scope="col">Pinned version {row.profile_version}</th>
        <th scope="col">Current version {row.current}</th>
      </tr></thead>
      <tbody>{row.role_comparison.map(choice => <tr key={JSON.stringify([choice.role, choice.field])}>
        <th scope="row">{choice.field ? choice.role + ' / ' + choice.field : choice.role}</th>
        <td>{roleValue(choice.pinned)}</td>
        <td>{roleValue(choice.current)}</td>
      </tr>)}</tbody>
    </table>
  </details>
}

export default function X07ProfilePortfolio({ binding, client, isCurrent, onConfirmed }) {
  const [shown, setShown] = useState(false)
  return shown
    ? <Portfolio binding={binding} client={client} isCurrent={isCurrent} onConfirmed={onConfirmed}
      onClose={() => setShown(false)} />
    : <button type="button" onClick={() => setShown(true)}>Review profile reuse across drawings</button>
}

function Portfolio({ binding, client, isCurrent, onClose, onConfirmed }) {
  const organizationId = binding.organization_id
  const heading = useId()
  const [projects, setProjects] = useState(null)
  const [projectId, setProjectId] = useState('')
  const [revisions, setRevisions] = useState([])
  const [selected, setSelected] = useState([])
  const [results, setResults] = useState(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const request = useRef(null)
  const comparePair = selected.length === 2
    && selected[0].revision.project_id === selected[1].revision.project_id
    && selected[0].revision.drawing_id === selected[1].revision.drawing_id
    && selected[0].sequence !== selected[1].sequence
    ? [...selected].sort((left, right) => left.sequence - right.sequence) : null
  const profileId = results?.[0]?.profile_id
  const throughVersion = results?.[0]?.current
  const canExport = uuid(profileId) && profileVersion(throughVersion)
    && results.every(row => row.profile_id === profileId)

  useEffect(() => () => request.current?.abort(), [])

  async function run(read, receive, failure) {
    if (!isCurrent()) return
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setPending(true)
    setError('')
    try {
      const data = await read(controller.signal)
      if (!controller.signal.aborted && isCurrent()) receive(data)
    } catch {
      if (!controller.signal.aborted && isCurrent()) setError(failure)
    } finally {
      if (!controller.signal.aborted && isCurrent()) setPending(false)
    }
  }
  function loadProjects() {
    return run(signal => listProjects(organizationId, { signal }), rows => {
      if (!Array.isArray(rows) || rows.some(row => !uuid(row?.project_id) || row.org_id !== organizationId
          || typeof row.name !== 'string' || !row.name.trim())) {
        throw new Error('Invalid project list')
      }
      setProjects(rows.filter(row => row.status === 'active'))
    }, 'Projects could not be loaded.')
  }
  // Read only when the user opens the review.
  const start = useRef(loadProjects)
  useEffect(() => { void start.current() }, [])

  function chooseProject(id) {
    request.current?.abort()
    setPending(false)
    setError('')
    setProjectId(id)
    setRevisions([])
    const project = projects.find(row => row.project_id === id)
    if (!project) return
    run(signal => openProject(id, organizationId, { signal }), data => {
      if (data?.project?.project_id !== id || data.project.org_id !== organizationId
          || !Array.isArray(data.drawing_versions)) throw new Error('Invalid project drawings')
      const drawingNames = new Map()
      for (const artifact of Array.isArray(data.drawing_artifacts) ? data.drawing_artifacts : []) {
        if (artifact?.org_id === organizationId && artifact.project_id === id && uuid(artifact.drawing_id)
            && typeof artifact.name === 'string' && artifact.name.trim()) {
          drawingNames.set(artifact.drawing_id, artifact.name)
        }
      }
      const drawingNumbers = new Map()
      const rows = data.drawing_versions.filter(row => row?.org_id === organizationId && row.project_id === id
        && uuid(row.version_id) && uuid(row.drawing_id) && Number.isSafeInteger(row.seq) && row.seq > 0)
      for (const row of rows) {
        if (!drawingNumbers.has(row.drawing_id)) drawingNumbers.set(row.drawing_id, drawingNumbers.size + 1)
      }
      setRevisions(rows.map(row => ({
        revision: { project_id: id, drawing_id: row.drawing_id, input_version_id: row.version_id },
        sequence: row.seq,
        label: [project.name, drawingNames.get(row.drawing_id) || 'Drawing ' + drawingNumbers.get(row.drawing_id),
          'Version ' + row.seq].join(' / '),
      })))
    }, 'Drawing versions could not be loaded.')
  }
  function toggle(item) {
    setResults(null)
    setSelected(items => items.some(row => row.revision.input_version_id === item.revision.input_version_id)
      ? items.filter(row => row.revision.input_version_id !== item.revision.input_version_id)
      : items.length < limit ? [...items, item] : items)
  }
  function review() {
    setResults(null)
    run(signal => reviewRoleProfileReuse(organizationId, selected.map(item => item.revision), { signal }),
      data => setResults(checkedResults(data, organizationId, selected)),
      'Profile reuse could not be reviewed. Try again.')
  }
  function download() {
    const snapshot = { profile_id: profileId, through_version: throughVersion }
    const payload = { ...snapshot, revisions: selected.map(item => item.revision) }
    run(signal => exportRemovedProfileRoles(organizationId, payload, { signal }),
      data => downloadRemovedRoles(data, organizationId, snapshot, selected),
      'Removed roles could not be downloaded. Try again.')
  }
  if (!isCurrent()) return null
  return <section aria-labelledby={heading}>
    <h3 id={heading}>Profile reuse across drawings</h3>
    <button type="button" onClick={onClose}>Close reuse review</button>
    <fieldset disabled={pending}>
      {projects && <label>Project <select value={projectId} onChange={event => chooseProject(event.target.value)}>
        <option value="">Choose a project</option>
        {projects.map(row => <option key={row.project_id} value={row.project_id}>{row.name}</option>)}
      </select></label>}
      {projects?.length === 0 && <p>No active projects available.</p>}
      {projectId && !revisions.length && !pending && !error && <p>No drawing versions in this project.</p>}
      {!!revisions.length && <fieldset><legend>Drawing versions</legend>
        {revisions.map(item => {
          const checked = selected.some(row => row.revision.input_version_id === item.revision.input_version_id)
          return <div key={item.revision.input_version_id}><label>
            <input type="checkbox" checked={checked} disabled={!checked && selected.length >= limit}
              onChange={() => toggle(item)} />{item.label}
          </label></div>
        })}
      </fieldset>}
      {selected.length === limit && <p>16 drawing versions selected. Remove one to add another.</p>}
      {!!selected.length && <ul aria-label="Selected drawing versions">{selected.map(item =>
        <li key={item.revision.input_version_id}>
          {item.label} <button type="button" aria-label={'Remove ' + item.label} onClick={() => toggle(item)}>Remove</button>
        </li>
      )}</ul>}
      {projects && <button type="button" disabled={!selected.length} onClick={review}>Check profile reuse</button>}
      {comparePair && <RoofPlacementComparison key={comparePair[0].revision.input_version_id
        + '/' + comparePair[1].revision.input_version_id} organizationId={organizationId} pair={comparePair}
        run={run} />}
      {comparePair && <PanelGroupComparison key={'groups/' + comparePair[0].revision.input_version_id
        + '/' + comparePair[1].revision.input_version_id} organizationId={organizationId} pair={comparePair}
        run={run} />}
      {selected.length > 0 && <RackPlacementReview key={selected[0].revision.project_id + '/'
        + selected[0].revision.drawing_id} binding={binding} client={client} pair={comparePair}
        run={run} onConfirmed={onConfirmed} />}
      {canExport && <button type="button" onClick={download}>
        Download removed roles for profile version {throughVersion}
      </button>}
    </fieldset>
    {pending && <p role="status">Loading drawing review data.</p>}
    {error && <p role="alert">{error}</p>}
    {!projects && error && <button type="button" onClick={loadProjects}>Retry projects</button>}
    {!!results?.length && <table>
      <caption>Profile reuse decisions</caption>
      <thead><tr><th scope="col">Drawing version</th><th scope="col">Decision</th><th scope="col">Profile</th></tr></thead>
      <tbody>{results.map(row => <tr key={row.input_version_id}>
        <th scope="row">{row.label}</th>
        <td>{statuses.get(row.status)}{row.reason && <div>{reasons.get(row.reason) ||
          (row.status === 'unavailable' ? 'The exact revision has no readable drawing intake.' : 'The saved roles require review.')}</div>}</td>
        <td>
          {row.profile_version === null ? 'None' : (row.pinned ? 'Pinned version ' : 'Current version ') + row.profile_version}
          {row.panel_comparison?.status === 'different' && <div>Selects different panels under the current version</div>}
          {row.role_comparison !== null && <RoleComparison row={row} />}
        </td>
      </tr>)}</tbody>
    </table>}
  </section>
}
