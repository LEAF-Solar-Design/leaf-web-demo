import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import X07ProjectProfileReview from './X07ProjectProfileReview.jsx'

const id = suffix => '00000000-0000-4000-8000-' + String(suffix).padStart(12, '0')
const org = id(1), projectA = id(2), projectB = id(3), drawingA = id(4), drawingB = id(5)
const profileId = id(6)
const a = { org_id: org, project_id: projectA, name: 'Canyon', status: 'active' }
const b = { org_id: org, project_id: projectB, name: 'Mesa', status: 'active' }
const versions = [1, 2, 3].map((seq, index) => ({
  org_id: org, project_id: index < 2 ? projectA : projectB,
  drawing_id: index < 2 ? drawingA : drawingB, version_id: id(10 + seq), seq,
}))
const nextVersion = { ...versions[1], version_id: id(14), seq: 3 }
const binding = { kind: 'project', organization_id: org, project_id: projectA, drawing_id: drawingA,
  input_version_id: versions[0].version_id }
const picks = versions.map(row => ({ project_id: row.project_id, drawing_id: row.drawing_id, input_version_id: row.version_id }))
const roleComparison = [
  { role: 'module', field: 'block', pinned: 'Panel', current: 'Edited' },
  { role: 'module', field: 'layer', pinned: 'PV', current: 'PV' },
  { role: 'roof', field: 'layer', pinned: null, current: 'NEW' },
]
const rows = [
  { ...picks[0], status: 'compatible', reason: null, profile_id: profileId, profile_version: 1,
    pinned: true, role_comparison: roleComparison, panel_comparison: { status: 'different', added: 2, removed: 2 } },
  { ...picks[1], status: 'needs_choice', reason: 'ROLE_NOT_FOUND', profile_id: profileId, profile_version: 1,
    pinned: true, role_comparison: roleComparison,
    panel_comparison: { status: 'unavailable', reason: 'CONFIRMED_WINDOW_UNAVAILABLE' } },
  { ...picks[2], status: 'unavailable', reason: 'SIP_R1_INTAKE_UNAVAILABLE', profile_id: profileId, profile_version: 2,
    pinned: false, role_comparison: null, panel_comparison: null },
]
const report = { customer_key: org, current_profile_version: 2, rows }
const snapshot = { profile_id: profileId, through_version: 2 }
const removedRoleReport = { customer_key: org, ...snapshot, rows: [
  { ...picks[0], status: 'uses_removed_roles', reason: null, pinned_profile_version: 1,
    removed_roles: [{ role: 'shade', applied: { layer: 'SHADE' } }] },
  { ...picks[1], status: 'no_removed_roles', reason: null, pinned_profile_version: 1, removed_roles: [] },
  { ...picks[2], status: 'unavailable', reason: 'SIP_R1_INTAKE_UNAVAILABLE',
    pinned_profile_version: null, removed_roles: [] },
] }
const response = (body, status = 200) => ({ ok: status === 200, status, json: async () => body })
const button = name => screen.getByRole('button', { name })

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

function mount(getReport = () => response(report), getExport = () => response(removedRoleReport), getRoofReport,
  { getRackReview, activeBinding = binding, pinned = true, extraVersion } = {}) {
  const profile = { customer_key: org, project_key: projectA, drawing_key: drawingA,
    input_version_id: activeBinding.input_version_id, expected_version: 1,
    application_record: pinned ? { customer_key: org, project_key: projectA, profile_id: profileId, version: 1,
      roles: { module: { block: 'Panel', layer: 'PV' } } } : null }
  const client = { readRoleProfile: vi.fn(async () => ({ ok: true,
    data: { ...profile, profile: profile.application_record } })),
    saveRoleProfile: vi.fn(), confirmRoleProfile: vi.fn() }
  const props = { projectClient: client, solarBinding: activeBinding, isCurrent: () => true, authorityKey: 'first' }
  const fetch = vi.fn(async (url, init) => {
    const path = new URL(url, 'https://test.invalid').pathname
    expect(init.headers['X-Org-Id']).toBe(org)
    if (path.endsWith('/placement-overrides/preview')) {
      expect([versions[1], ...(extraVersion ? [extraVersion] : [])].map(row => '/api/projects/' + projectA
        + '/drawings/' + drawingA + '/versions/' + row.version_id + '/role-profile/placement-overrides/preview')).toContain(path)
      expect(init.headers['X-Project-Id']).toBe(projectA)
      expect(init.method).toBe('POST')
      expect(init.cache).toBe('no-store')
      return getRackReview(JSON.parse(init.body), init.signal, path)
    }
    if (path.endsWith('/placement-changes/preview')) {
      expect(path).toBe('/api/projects/' + projectA + '/drawings/' + drawingA + '/versions/'
        + versions[1].version_id + '/role-profile/placement-changes/preview')
      expect(init.headers['X-Project-Id']).toBe(projectA)
      expect(init.method).toBe('POST')
      expect(init.cache).toBe('no-store')
      expect(JSON.parse(init.body)).toEqual({
        previous_input_version_id: versions[0].version_id, block: 'Panel', layer: 'PV',
        windows: ['E1', 'E2'].map(outline_handle => ({ within: { outline_handle } })),
      })
      return getRoofReport(init)
    }
    expect(init.headers['X-Project-Id']).toBeUndefined()
    if (path === '/api/projects') return response({ projects: [a, b] })
    if (path === '/api/projects/' + projectA) return response({ project: a,
      drawing_versions: [...versions.slice(0, 2), ...(extraVersion ? [extraVersion] : [])],
      drawing_artifacts: [{ org_id: org, project_id: projectA, drawing_id: drawingA, name: 'Roof plan' }] })
    if (path === '/api/projects/' + projectB) return response({ project: b, drawing_versions: versions.slice(2) })
    if (path === '/api/role-profiles/reuse-review/export') {
      expect(init.method).toBe('POST')
      expect(init.cache).toBe('no-store')
      expect(JSON.parse(init.body)).toEqual({ ...snapshot, revisions: picks })
      return getExport(init)
    }
    expect(path).toBe('/api/role-profiles/reuse-review')
    expect(init.method).toBe('POST')
    expect(init.cache).toBe('no-store')
    expect(JSON.parse(init.body)).toEqual({ revisions: picks })
    return getReport(init)
  })
  vi.stubGlobal('fetch', fetch)
  const rendered = render(<X07ProjectProfileReview {...props} />)
  return { rendered, fetch, props }
}

async function selectPortfolio() {
  fireEvent.click(button('Review profile reuse across drawings'))
  const picker = await screen.findByRole('combobox', { name: 'Project' })
  fireEvent.change(picker, { target: { value: projectA } })
  for (const seq of [1, 2]) {
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Canyon / Roof plan / Version ' + seq }))
  }
  fireEvent.change(picker, { target: { value: projectB } })
  fireEvent.click(await screen.findByRole('checkbox', { name: 'Mesa / Drawing 1 / Version 3' }))
  fireEvent.click(button('Check profile reuse'))
}

it('uses the authorized project and revision reads and explains all three decisions without editing pins', async () => {
  const { fetch, props } = mount()
  expect(fetch).not.toHaveBeenCalled()
  await selectPortfolio()
  const table = await screen.findByRole('table', { name: 'Profile reuse decisions' })
  const canyon = within(table).getByRole('row', { name: /Canyon.*Version 1/ })
  expect(canyon.textContent).toContain('Can reuse saved roles')
  expect(canyon.textContent).toContain('Pinned version 1')
  expect(canyon.textContent).toContain('Selects different panels under the current version')
  fireEvent.click(within(canyon).getByText('Compare with current version 2'))
  expect(within(canyon).getByText('Different panels in the confirmed window. Added 2, removed 2.')).toBeTruthy()
  const comparison = within(canyon).getByRole('table', { name: 'Saved role choices' })
  expect(within(comparison).getByRole('columnheader', { name: 'Pinned version 1' })).toBeTruthy()
  expect(within(comparison).getByRole('columnheader', { name: 'Current version 2' })).toBeTruthy()
  for (const [label, values] of [
    ['module / block', ['Panel', 'Edited']],
    ['module / layer', ['PV', 'PV']],
    ['roof / layer', ['?', 'NEW']],
  ]) {
    const role = within(comparison).getByRole('row', { name: new RegExp(label) })
    expect(within(role).getAllByRole('cell').map(cell => cell.textContent)).toEqual(values)
  }
  const canyon2 = within(table).getByRole('row', { name: /Canyon.*Version 2/ })
  expect(canyon2.textContent).toContain('Needs a new role choice')
  expect(canyon2.textContent).toContain('A saved block or layer is missing')
  expect(within(canyon2).getByText('Compare with current version 2')).toBeTruthy()
  expect(within(canyon2).getByText('This drawing version has no recorded confirmation window.')).toBeTruthy()
  const mesa = within(table).getByRole('row', { name: /Mesa.*Version 3/ })
  expect(mesa.textContent).toContain('Drawing data unavailable')
  expect(mesa.textContent).toContain('Current version 2')
  expect(within(mesa).queryByText('Compare with current version 2')).toBeNull()
  expect(fetch.mock.calls.map(([, init]) => init.method || 'GET')).toEqual(['GET', 'GET', 'GET', 'POST'])
  expect(props.projectClient.saveRoleProfile).not.toHaveBeenCalled()
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
})

it.each([
  { ...report, customer_key: id(999) },
  { ...report, rows: [rows[0], rows[0], rows[2]] },
  { ...report, rows: rows.slice(0, 2) },
  { ...report, current_profile_version: 1 },
  { ...report, rows: [{ ...rows[0], role_comparison: null }, ...rows.slice(1)] },
  { ...report, rows: [{ ...rows[0], panel_comparison: { status: 'different', added: 0, removed: 0 } }, ...rows.slice(1)] },
])('refuses the entire mismatched result and can retry with the original selection', async bad => {
  const getReport = vi.fn().mockImplementationOnce(() => response(bad)).mockImplementation(() => response(report))
  mount(getReport)
  await selectPortfolio()
  const review = screen.getByRole('region', { name: 'Profile reuse across drawings' })
  expect((await within(review).findByRole('alert')).textContent).toContain('Profile reuse could not be reviewed')
  expect(screen.queryByRole('table')).toBeNull()
  fireEvent.click(button('Check profile reuse'))
  await screen.findByRole('table', { name: 'Profile reuse decisions' })
  expect(getReport).toHaveBeenCalledTimes(2)
})

it('retires a pending report when workspace authority changes', async () => {
  let resolve
  const getReport = vi.fn(() => new Promise(done => { resolve = done }))
  const { rendered, props } = mount(getReport)
  await selectPortfolio()
  const { signal } = getReport.mock.calls[0][0]
  rendered.rerender(<X07ProjectProfileReview {...props} authorityKey="second" />)
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(response(report)))
  expect(screen.queryByRole('table')).toBeNull()
  expect(button('Review profile reuse across drawings')).toBeTruthy()
})

const downloadName = 'Download removed roles for profile version 2'

function observeDownload() {
  const create = vi.fn(() => 'blob:test/removed-roles')
  const revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = create
    static revokeObjectURL = revoke
  })
  const downloads = []
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () {
    downloads.push({ filename: this.download, url: this.getAttribute('href') })
  })
  return { create, revoke, downloads }
}

it('downloads the reviewed profile version and selected revisions with their Studio labels', async () => {
  const { props } = mount()
  const { create, revoke, downloads } = observeDownload()
  expect(screen.queryByRole('button', { name: downloadName })).toBeNull()
  await selectPortfolio()
  fireEvent.click(await screen.findByRole('button', { name: downloadName }))
  await waitFor(() => expect(downloads).toEqual([
    { filename: 'removed-profile-roles.json', url: 'blob:test/removed-roles' },
  ]))
  expect(revoke).toHaveBeenCalledWith('blob:test/removed-roles')
  const blob = create.mock.calls[0][0]
  expect(blob.type).toBe('application/json')
  const content = await new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
  const labels = ['Canyon / Roof plan / Version 1', 'Canyon / Roof plan / Version 2', 'Mesa / Drawing 1 / Version 3']
  expect(JSON.parse(content)).toEqual({ ...removedRoleReport,
    rows: removedRoleReport.rows.map((row, index) => ({ ...row, label: labels[index] })) })
  expect(props.projectClient.saveRoleProfile).not.toHaveBeenCalled()
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
})

it.each([
  { ...removedRoleReport, customer_key: id(999) },
  { ...removedRoleReport, through_version: 3 },
  { ...removedRoleReport, rows: [removedRoleReport.rows[1], removedRoleReport.rows[0], removedRoleReport.rows[2]] },
])('keeps results and allows retry after rejecting a mismatched export', async bad => {
  const getExport = vi.fn().mockImplementationOnce(() => response(bad))
    .mockImplementation(() => response(removedRoleReport))
  mount(undefined, getExport)
  const { downloads } = observeDownload()
  await selectPortfolio()
  fireEvent.click(await screen.findByRole('button', { name: downloadName }))
  expect((await screen.findByRole('alert')).textContent).toContain('Removed roles could not be downloaded')
  expect(downloads).toHaveLength(0)
  expect(screen.getByRole('table', { name: 'Profile reuse decisions' })).toBeTruthy()
  fireEvent.click(button(downloadName))
  await waitFor(() => expect(downloads).toHaveLength(1))
  expect(getExport).toHaveBeenCalledTimes(2)
})

it('ignores an export response after a change in workspace authority', async () => {
  let resolve
  const getExport = vi.fn(() => new Promise(done => { resolve = done }))
  const { rendered, props } = mount(undefined, getExport)
  const { create, downloads } = observeDownload()
  await selectPortfolio()
  const download = await screen.findByRole('button', { name: downloadName })
  fireEvent.click(download)
  expect(download.closest('fieldset').disabled).toBe(true)
  const { signal } = getExport.mock.calls[0][0]
  rendered.rerender(<X07ProjectProfileReview {...props} authorityKey="second" />)
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(response(removedRoleReport)))
  expect(create).not.toHaveBeenCalled()
  expect(downloads).toHaveLength(0)
})

const roofReport = (before, after, retained, added, removed) => ({
  previous_input_version_id: versions[0].version_id,
  input_version_id: versions[1].version_id,
  windows: [{
    previous_count: before, current_count: after, retained_count: retained,
    count_delta: after - before, added_paths: added, removed_paths: removed,
  }, {
    previous_count: 4, current_count: 4, retained_count: 4,
    count_delta: 0, added_paths: [], removed_paths: [],
  }],
})

async function selectRevisionPair() {
  fireEvent.click(button('Review profile reuse across drawings'))
  const picker = await screen.findByRole('combobox', { name: 'Project' })
  fireEvent.change(picker, { target: { value: projectA } })
  for (const seq of [2, 1]) {
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Canyon / Roof plan / Version ' + seq }))
  }
}

async function selectRoofComparison() {
  await selectRevisionPair()
  const roof = within(screen.getByRole('group', { name: 'Roof panel changes' }))
  fireEvent.change(roof.getByRole('textbox', { name: 'Module block' }), { target: { value: 'Panel' } })
  fireEvent.change(roof.getByRole('textbox', { name: 'Module layer' }), { target: { value: 'PV' } })
  const outlines = roof.getByRole('textbox', { name: 'Roof outline handles' })
  fireEvent.change(outlines, { target: { value: 'e1, E1' } })
  expect(button('Compare roof panels').disabled).toBe(true)
  fireEvent.change(outlines, { target: { value: 'e1, e2' } })
  fireEvent.click(button('Compare roof panels'))
  return outlines
}

it.each([
  ['array reduction', roofReport(4, 2, 2, [], ['B1[1,0]', 'B1[1,1]']),
    ['4', '2', '-2', '2', 'None', 'B1[1,0]B1[1,1]']],
  ['rack enters outline', roofReport(2, 3, 2, ['B1[1,0]'], []),
    ['2', '3', '+1', '2', 'B1[1,0]', 'None']],
  ['child replacement', roofReport(4, 4, 3, ['B1[0,0]/C2[0,0]'], ['B1[0,0]/C1[0,0]']),
    ['4', '4', '0', '3', 'B1[0,0]/C2[0,0]', 'B1[0,0]/C1[0,0]']],
])('shows exact per-roof paths and counts for %s in revision order', async (_name, data, expected) => {
  const { props } = mount(undefined, undefined, () => response(data))
  const outlines = await selectRoofComparison()
  const table = await screen.findByRole('table', { name: 'Roof panel changes by outline' })
  const cells = roof => within(within(table).getByRole('row', { name: new RegExp('^' + roof + ' ') }))
    .getAllByRole('cell').map(cell => cell.textContent)
  expect(cells('E1')).toEqual(expected)
  expect(cells('E2')).toEqual(['4', '4', '0', '4', 'None', 'None'])
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
  expect(props.projectClient.saveRoleProfile).not.toHaveBeenCalled()
  fireEvent.change(outlines, { target: { value: 'E1' } })
  expect(screen.queryByRole('table', { name: 'Roof panel changes by outline' })).toBeNull()
})

it('refuses a result from a different revision and retires a pending retry on scope change', async () => {
  const good = roofReport(4, 4, 4, [], [])
  let resolve
  const getRoofReport = vi.fn()
    .mockImplementationOnce(() => response({ ...good, input_version_id: versions[2].version_id }))
    .mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const { rendered, props } = mount(undefined, undefined, getRoofReport)
  await selectRoofComparison()
  expect((await screen.findByRole('alert')).textContent).toContain('Roof panel changes could not be compared')
  expect(screen.queryByRole('table')).toBeNull()
  fireEvent.click(button('Compare roof panels'))
  await waitFor(() => expect(getRoofReport).toHaveBeenCalledTimes(2))
  const { signal } = getRoofReport.mock.calls[1][0]
  rendered.rerender(<X07ProjectProfileReview {...props} authorityKey="second" />)
  expect(signal.aborted).toBe(true)
  await act(async () => resolve(response(good)))
  expect(screen.queryByRole('table')).toBeNull()
})

const currentBinding = { ...binding, input_version_id: versions[1].version_id }
const overridePaths = [['B1[0,0]/C1[0,0]', 'B1[1,0]/C1[0,0]'], ['B2[0,0]/C1[0,0]']]
const rackWindow = (paths, index) => ({ within: { outline_handle: index ? 'E2' : 'E1' }, placement_paths: paths })
const requestedRacks = overridePaths.map(rackWindow)

const rackPreview = (firstStatuses = ['unchanged', 'missing']) => ({
  previous_input_version_id: versions[0].version_id, input_version_id: versions[1].version_id,
  overrides: [firstStatuses, ['unchanged']].map((statuses, index) => ({
    selection: { design_window: requestedRacks[index] },
    placements: statuses.map((status, i) => ({ path: overridePaths[index][i], status,
      previous_origin: [2, 3], current_origin: status === 'missing' ? null : status === 'moved' ? [5, 7] : [2, 3] })),
  })),
})

async function previewRacks() {
  await selectRevisionPair()
  const set = (group, name, value) => fireEvent.change(group.getByRole('textbox', { name }), { target: { value } })
  const review = within(screen.getByRole('group', { name: 'Rack override review' }))
  set(review, 'Override module block', 'Panel')
  set(review, 'Override module layer', 'PV')
  fireEvent.click(button('Add rack override'))
  for (const index of [0, 1]) {
    const rack = within(review.getByRole('group', { name: 'Rack override ' + (index + 1) }))
    set(rack, 'Roof outline handle', 'e' + (index + 1))
    set(rack, 'Earlier placement paths', overridePaths[index].join('\n').toLowerCase())
  }
  fireEvent.click(button('Preview rack overrides'))
}

it.each([
  ['array row reduction', ['unchanged', 'missing'], ['Still matches', 'Missing'], [overridePaths[0][0]]],
  ['translated rotated rack', ['moved', 'moved'], ['Moved and still matches', 'Moved and still matches'], overridePaths[0]],
  ['nested child replacement', ['missing', 'missing'], ['Missing', 'Missing'], []],
])('reviews %s and confirms only current matches after an explicit action', async (_name, statuses, labels, paths) => {
  const getRackReview = vi.fn(() => response(rackPreview(statuses)))
  const { props } = mount(undefined, undefined, undefined, { getRackReview, activeBinding: currentBinding, pinned: false })
  const window = rackWindow(paths, 0)
  props.projectClient.confirmRoleProfile.mockResolvedValue({ ok: true,
    data: { customer_key: org, project_key: projectA, drawing_key: drawingA, input_version_id: currentBinding.input_version_id,
      selection: { design_window: window } } })
  await previewRacks()
  const table = await screen.findByRole('table', { name: 'Rack override 1 review' })
  const rows = within(table).getAllByRole('row').slice(1).map(row => within(row).getAllByRole('cell')[0].textContent)
  expect(rows).toEqual(labels)
  expect(within(screen.getByRole('table', { name: 'Rack override 2 review' })).getByText('Still matches')).toBeTruthy()
  expect(getRackReview.mock.calls[0][0]).toEqual({
    previous_input_version_id: versions[0].version_id, block: 'Panel', layer: 'PV', overrides: requestedRacks,
  })
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
  const confirm = button('Confirm ' + paths.length + ' matching placements for rack override 1')
  if (!paths.length) {
    expect(confirm.disabled).toBe(true)
    return
  }
  fireEvent.click(confirm)
  expect(await screen.findByText('Confirmed ' + paths.length + ' placements for rack override 1.')).toBeTruthy()
  expect(props.projectClient.confirmRoleProfile).toHaveBeenCalledTimes(1)
  expect(props.projectClient.confirmRoleProfile).toHaveBeenCalledWith({ block: 'Panel', layer: 'PV', design_window: window },
    { expected_version: 1, signal: expect.any(AbortSignal) })
})

it.each([
  [true, [2, 3], 'Unchanged'],
  [false, [5, 7], 'Moved'],
  [true, [5, 7], 'Moved'],
])('shows outline change %s and the panel origin for a path outside the revised outline', async (boundaryChanged, origin, panelMotion) => {
  const data = rackPreview()
  data.overrides[0].boundary_changed = boundaryChanged
  data.overrides[0].placements[1].current_drawing_origin = origin
  const { props } = mount(undefined, undefined, undefined, {
    getRackReview: () => response(data), activeBinding: currentBinding, pinned: false,
  })
  await previewRacks()
  const table = await screen.findByRole('table', { name: 'Rack override 1 review' })
  const removed = within(table).getByRole('row', { name: /Outside revised outline/ })
  expect(within(removed).getAllByRole('cell').map(cell => cell.textContent))
    .toEqual(['Outside revised outline', '2, 3', origin.join(', '), panelMotion])
  expect(within(table.parentElement).getByText('Roof outline ' + (boundaryChanged ? 'changed' : 'unchanged') + '.')).toBeTruthy()
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
})

it('keeps earlier paths when reopening against a later revision and requires the active revision to confirm', async () => {
  const getRackReview = vi.fn((_payload, _signal, path) => response(path.includes(nextVersion.version_id)
    ? { ...rackPreview(['moved', 'missing']), input_version_id: nextVersion.version_id } : rackPreview()))
  const { props } = mount(undefined, undefined, undefined, { getRackReview, extraVersion: nextVersion })
  await previewRacks()
  const message = 'Open the newer drawing version in the workspace to confirm.'
  expect(await screen.findByText(message)).toBeTruthy()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Canyon / Roof plan / Version 2' }))
  expect(screen.queryByText(message)).toBeNull()
  fireEvent.click(screen.getByRole('checkbox', { name: 'Canyon / Roof plan / Version 3' }))
  expect(screen.queryByRole('table', { name: 'Rack override 1 review' })).toBeNull()
  fireEvent.click(button('Preview rack overrides'))
  expect(await screen.findByText(message)).toBeTruthy()
  expect(getRackReview.mock.calls[1][0].overrides).toEqual(requestedRacks)
  expect(getRackReview.mock.calls[1][2]).toContain(nextVersion.version_id)
  expect(within(screen.getByRole('table', { name: 'Rack override 1 review' }))
    .getByText('Moved and still matches')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /Confirm .* matching placements/ })).toBeNull()
  expect(props.projectClient.confirmRoleProfile).not.toHaveBeenCalled()
})

it('rejects override evidence returned for the wrong roof', async () => {
  const data = rackPreview()
  data.overrides[0].selection.design_window = rackWindow(overridePaths[0], 1)
  mount(undefined, undefined, undefined, { getRackReview: () => response(data), activeBinding: currentBinding })
  await previewRacks()
  expect((await screen.findByRole('alert')).textContent).toContain('Rack overrides could not be compared')
  expect(screen.queryByRole('table', { name: 'Rack override 1 review' })).toBeNull()
})
