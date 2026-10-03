import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarTrackerRowsPanel, { TRACKER_ROWS_PANEL_REASONS as S } from './SolarTrackerRowsPanel.jsx'
import { TRACKER_ROWS_REASONS as R } from './solarTrackerRowsReasons.js'
import { TRACKER_ROWS_CLIENT_REASONS as C } from './solarTrackerRowsClient.js'
import * as model from './solarTrackerRowsModel.js'

const H = 'a'.repeat(64)
const H2 = 'b'.repeat(64)
const row = () => ({ axis_start: [0, 0], axis_end: [0, 6], cross_axis_width_du: 2, slots: 3 })
const m1 = () => ({ operation: 'manual-create', rows: [row(), {
  axis_start: [4, 0], axis_end: [4, 10], cross_axis_width_du: 1, slots: 2,
}], module_power_watts: 450, expected_head: null })
const frame = (units = 'm') => ({ coordinate_system: 'world', transform: 'identity', drawing_units: units,
  meters_per_unit: units === 'm' ? 1 : 0.3048, crs: 'none', elevation_datum: 'unrecorded',
  horizontal: 'drawing-units', elevation: 'metres' })
const head = (id = H, index = 0, parent = null) => ({ schema: 'leaf.solar-physical-head.v1',
  drawing_id: 'solar', project_id: 'p', index, parent,
  state: { schema: 'leaf.solar-artifact-ref.v1', artifact_id: id, content_sha256: H,
    media_type: 'application/json', filename: 'physical-state.json', byte_length: 100, source_version: 1,
    download: `/api/drawings/solar/artifacts/${id}` },
})
const absent = () => ({ ok: true, value: { schema: 'leaf.solar-terrain-view-response.v1',
  stored: false, head: null, terrain: null } })
const stored = (h = head(), units = 'm', grid = null) => ({ ok: true, value: {
  schema: 'leaf.solar-terrain-view-response.v1', stored: true, head: h,
  terrain: { schema: 'leaf.solar-terrain-view.v1', maturity: 'preview', drawing_id: 'solar', project_id: 'p',
    frame: frame(units), grid, mesh_faces: 0, slope_markers: 0, previews: {
      'terrain-mesh-render': { state: 'absent', record: null },
      'tracker-slope-violations': { state: 'absent', record: null },
    } },
} })
const intake = (units = 'm') => ({ version: 1, head: 1, intake: { solar_design_graph: {
  project: { id: 'p', units: { drawing_units: units, meters_per_unit: units === 'm' ? 1 : 0.3048 } },
} } })
function success(request = m1(), status = 201) {
  return { ok: true, status, value: { schema: 'leaf.solar-tracker-rows.v1', operation: 'manual-create',
    outcome: status === 201 ? 'published' : 'retry', created: status === 201, drawing_id: 'solar', project_id: 'p',
    expected_head: request.expected_head, head: head(H, request.expected_head === null ? 0 : 1, request.expected_head),
    frame: frame(), summary: { rows: request.rows.length, slots: request.rows.reduce((n, r) => n + r.slots, 0),
      module_power_watts: request.module_power_watts },
    terrain_standing: { schema: 'leaf.solar-frames-piles-terrain-standing.v1', maturity: 'preview', grid_sha256: null,
      frames: { state: 'absent', checked: 0, stale: 0 }, piles: { state: 'absent', checked: 0, stale: 0 } },
  } }
}
function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}
function props(overrides = {}) {
  return { drawingId: 'solar', projectId: 'p', drawingVersion: 1, active: true, checkoutHeld: true, busy: false,
    client: { createTrackerRows: vi.fn(async ({ request }) => success(request)) },
    terrainClient: { getTerrain: vi.fn(async () => absent()) }, readIntake: vi.fn(async () => intake()),
    onPhysicalHeadChanged: vi.fn(), onClose: vi.fn(), ...overrides }
}
const panel = () => screen.getByTestId('solar-tracker-rows-panel')
const phase = () => panel().getAttribute('data-phase')
const publish = () => screen.getByRole('button', { name: 'Publish tracker rows' })
const refresh = () => screen.getByRole('button', { name: 'Refresh physical state' })
const retry = () => screen.getByRole('button', { name: 'Retry original request' })
const change = (input, value) => fireEvent.change(input, { target: { value: String(value) } })
const ready = () => waitFor(() => expect(['invalid', 'ready', 'disabled']).toContain(phase()))
function fillRow(index, value = row()) {
  const group = within(screen.getByRole('group', { name: `Row ${index + 1}`, exact: true }))
  for (const [label, number] of [['Axis start X', value.axis_start[0]], ['Axis start Y', value.axis_start[1]],
    ['Axis end X', value.axis_end[0]], ['Axis end Y', value.axis_end[1]],
    ['Cross axis width', value.cross_axis_width_du], ['Slots', value.slots]]) change(group.getByLabelText(label), number)
}
function fill(two = true) {
  change(screen.getByLabelText('Module power'), '450')
  fillRow(0)
  if (two) { fireEvent.click(screen.getByRole('button', { name: 'Add row' })); fillRow(1, m1().rows[1]) }
}
async function setup(overrides = {}, two = true) {
  const p = props(overrides)
  const view = render(<SolarTrackerRowsPanel {...p} />)
  await ready()
  fill(two)
  return { p, view }
}
async function submit(p, expected = 'published') {
  fireEvent.click(publish())
  await waitFor(() => expect(phase()).toBe(expected))
  expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

describe('manual tracker rows panel', () => {
  it('p01_no_drawing', () => {
    const p = props({ drawingId: null })
    render(<SolarTrackerRowsPanel {...p} />)
    expect(phase()).toBe('no-drawing')
    expect(panel().textContent).toBe(R.TRACKER_ROWS_DRAWING_NOT_FOUND)
    expect(screen.queryByRole('button')).toBeNull()
    expect(p.terrainClient.getTerrain).not.toHaveBeenCalled()
    expect(p.readIntake).not.toHaveBeenCalled()
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p02_unusable_client', () => {
    for (const override of [{ client: {} }, { terrainClient: {} }, { readIntake: null }]) {
      const p = props(override)
      render(<SolarTrackerRowsPanel {...p} />)
      expect(phase()).toBe('unavailable')
      expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(C.TRACKER_ROWS_CLIENT_REQUEST_INVALID)
      expect(publish().disabled).toBe(true)
      expect(refresh().disabled).toBe(true)
      expect(p.readIntake?.mock?.calls ?? []).toHaveLength(0)
      cleanup()
    }
  })
  it('p03_absent_head_units', async () => {
    const p = props()
    render(<SolarTrackerRowsPanel {...p} />)
    await ready()
    expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
    expect(p.readIntake).toHaveBeenCalledExactlyOnceWith('solar', 'head')
    expect(screen.getByText(S.no_head)).toBeTruthy()
    expect(screen.getByText('Metres')).toBeTruthy()
    expect(screen.queryByText('Current head index')).toBeNull()
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p04_existing_head', async () => {
    const grid = { rows: 2, cols: 2, x_min: 0, x_max: 10, y_min: 0, y_max: 10,
      cell_x: 10, cell_y: 10, cell_x_m: 10, cell_y_m: 10,
      elevation_min_m: 0, elevation_max_m: 0, grid_sha256: H }
    for (const variant of [grid, null]) {
      const { p } = await setup({ terrainClient: { getTerrain: vi.fn(async () => stored(head(), 'm', variant)) } })
      expect(phase()).toBe('ready')
      expect(panel().getAttribute('data-head')).toBe(H)
      expect(p.readIntake).not.toHaveBeenCalled()
      expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('p05_context_read_failure', async () => {
    for (const overrides of [
      { terrainClient: { getTerrain: vi.fn(async () => ({ ok: false })) } },
      { terrainClient: { getTerrain: vi.fn(async () => ({ ok: true, value: {} })) } },
      { readIntake: vi.fn(async () => { throw new Error('private') }) },
    ]) {
      const p = props(overrides)
      render(<SolarTrackerRowsPanel {...p} />)
      await waitFor(() => expect(phase()).toBe('read-refused'))
      expect(panel().textContent).toContain(overrides.readIntake ? S.units_unreadable : S.read_failed)
      expect(publish().disabled).toBe(true)
      expect(refresh().disabled).toBe(false)
      expect(p.client.createTrackerRows).not.toHaveBeenCalled()
      cleanup()
    }
    vi.useFakeTimers()
    const p = props({ readIntake: vi.fn(() => new Promise(() => {})) })
    render(<SolarTrackerRowsPanel {...p} />)
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    await act(async () => { await vi.advanceTimersByTimeAsync(120000) })
    expect(phase()).toBe('read-refused')
    expect(panel().textContent).toContain(S.units_unreadable)
  })
  it('p06_context_scope_and_units', async () => {
    const wrong = intake(); wrong.intake.solar_design_graph.project.id = 'other'
    const missing = intake(); delete missing.intake.solar_design_graph
    for (const [value, version, text] of [[wrong, 1, R.TRACKER_ROWS_PROJECT_MISMATCH],
      [intake('in'), 1, R.TRACKER_ROWS_UNITS_UNSUPPORTED], [missing, 1, R.TRACKER_ROWS_GRAPH_REQUIRED],
      [intake(), 2, S.drawing_refresh]]) {
      const p = props({ readIntake: vi.fn(async () => value), drawingVersion: version })
      render(<SolarTrackerRowsPanel {...p} />)
      await waitFor(() => expect(phase()).toBe('read-refused'))
      expect(panel().textContent).toContain(text)
      expect(p.client.createTrackerRows).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('p07_blank_initial_drafts', async () => {
    render(<SolarTrackerRowsPanel {...props()} />)
    await ready()
    expect(phase()).toBe('invalid')
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_POWER_INVALID)
    for (const input of screen.getAllByRole('textbox')) expect(input.value).toBe('')
    expect(screen.getAllByRole('group', { name: /^Row / })).toHaveLength(1)
    expect(publish().disabled).toBe(true)
  })
  it('p08_two_rows_request', async () => {
    const { p } = await setup()
    const readsAtSend = []
    p.client.createTrackerRows.mockImplementation(async ({ request }) => {
      readsAtSend.push(p.terrainClient.getTerrain.mock.calls.length)
      return success(request)
    })
    await submit(p)
    const sent = p.client.createTrackerRows.mock.calls[0][0]
    expect(sent.request).toEqual(m1())
    expect(Object.keys(sent.request)).toEqual(['operation', 'rows', 'module_power_watts', 'expected_head'])
    expect(sent).toMatchObject({ drawingId: 'solar', projectId: 'p', drawingUnits: 'm' })
    expect(readsAtSend).toEqual([1])
  })
  it('p09_decimal_and_signed_zero', async () => {
    const { p } = await setup({}, false)
    change(screen.getByLabelText('Module power'), ' 4.50e2 ')
    fillRow(0, { axis_start: ['-0', '+0e2'], axis_end: ['.0', '6.'], cross_axis_width_du: '2e0', slots: '3.0' })
    await submit(p)
    expect(p.client.createTrackerRows.mock.calls[0][0].request).toEqual({ ...m1(), rows: [row()] })
    expect(Object.is(p.client.createTrackerRows.mock.calls[0][0].request.rows[0].axis_start[0], -0)).toBe(false)
  })
  it('p10_row_count_controls', async () => {
    const { p } = await setup({}, false)
    fireEvent.click(screen.getByRole('button', { name: 'Remove row' }))
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_ROWS_INVALID)
    const add = screen.getByRole('button', { name: 'Add row' })
    for (let i = 0; i < 256; i += 1) fireEvent.click(add)
    expect(screen.getAllByRole('group', { name: /^Row / })).toHaveLength(256)
    expect(screen.getByRole('button', { name: 'Add row' }).disabled).toBe(true)
    expect(panel().textContent).toContain(R.TRACKER_ROWS_LIMIT_EXCEEDED)
    fireEvent.click(screen.getByRole('button', { name: 'Add row' }))
    expect(screen.getAllByRole('group', { name: /^Row / })).toHaveLength(256)
    const builder = vi.spyOn(model, 'buildTrackerRowsRequest').mockReturnValue({ ok: false,
      code: 'TRACKER_ROWS_LIMIT_EXCEEDED', field: 'rows' })
    change(screen.getByLabelText('Module power'), '451')
    fireEvent.click(publish())
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
    builder.mockRestore()
    // Reaching the 256-row cap through the form re-renders every row on each click: about 20 s in
    // jsdom, measured, so this row carries its own budget instead of the 5 s default.
  }, 120_000)
  it('p11_power_validation_order', async () => {
    const { p } = await setup({}, false)
    change(screen.getByLabelText('Module power'), '0')
    change(screen.getByLabelText('Axis start X'), '0x1')
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_POWER_INVALID)
    expect(screen.getByLabelText('Module power').getAttribute('aria-invalid')).toBe('true')
    fireEvent.click(publish())
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p12_coordinate_validation', async () => {
    const { p } = await setup({}, false)
    for (const number of ['', '0x1', 'Infinity', '1000000001']) {
      change(screen.getByLabelText('Axis start X'), number)
      const input = screen.getByLabelText('Axis start X')
      expect(input.getAttribute('aria-invalid')).toBe('true')
      expect(document.getElementById(input.getAttribute('aria-describedby')).textContent).toBe(R.TRACKER_ROWS_ROW_INVALID)
      fireEvent.click(publish())
    }
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p13_slot_validation', async () => {
    const { p } = await setup({}, false)
    for (const slots of ['0', '1.5', '10001', '1', '10000']) {
      change(screen.getByLabelText('Slots'), slots)
      expect(publish().disabled).toBe(!['1', '10000'].includes(slots))
      if (publish().disabled) {
        expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_SLOTS_INVALID)
        fireEvent.click(publish())
      }
    }
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p14_total_slot_order', async () => {
    const { p } = await setup({}, false)
    fillRow(0, { ...row(), slots: 10000 })
    for (let i = 1; i < 11; i += 1) {
      fireEvent.click(screen.getByRole('button', { name: 'Add row' }))
      fillRow(i, { ...row(), slots: 10000, axis_end: i === 10 ? [0, 0] : [0, 6] })
    }
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_LIMIT_EXCEEDED)
    fireEvent.click(publish())
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p15_axis_boundaries', async () => {
    for (const [units, lengths] of [['m', [0, 1e-9, 1.0000000000000003e-9, 100000, 100000.00000000001]],
      ['ft', [328083, 328084]]]) {
      const { p } = await setup({ terrainClient: { getTerrain: vi.fn(async () => stored(head(), units)) } }, false)
      for (const length of lengths) {
        change(screen.getByLabelText('Axis end Y'), length)
        const accepted = length > 1e-9 && length * (units === 'm' ? 1 : 0.3048) <= 100000
        expect(publish().disabled).toBe(!accepted)
        if (!accepted) {
          expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_AXIS_INVALID)
          fireEvent.click(publish())
        }
      }
      expect(p.client.createTrackerRows).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('p16_width_boundaries', async () => {
    for (const [units, widths] of [['m', [-1, 0, 1e-9, 1.0000000000000003e-9, 1000, 1000.0000000000001]],
      ['ft', [3280, 3281]]]) {
      const { p } = await setup({ terrainClient: { getTerrain: vi.fn(async () => stored(head(), units)) } }, false)
      for (const width of widths) {
        change(screen.getByLabelText('Cross axis width'), width)
        const accepted = width > 1e-9 && width * (units === 'm' ? 1 : 0.3048) <= 1000
        expect(publish().disabled).toBe(!accepted)
        if (!accepted) {
          expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_WIDTH_INVALID)
          fireEvent.click(publish())
        }
      }
      expect(p.client.createTrackerRows).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('p17_checkout_busy_precedence', async () => {
    const { p, view } = await setup({ busy: true, checkoutHeld: false })
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.run_pending)
    view.rerender(<SolarTrackerRowsPanel {...p} busy={false} />)
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(R.TRACKER_ROWS_CHECKOUT_REQUIRED)
    fireEvent.click(refresh())
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
    fireEvent.click(publish())
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p18_double_submit', async () => {
    const held = deferred()
    const { p } = await setup({ client: { createTrackerRows: vi.fn(() => held.promise) } })
    act(() => { fireEvent.click(publish()); fireEvent.click(publish()) })
    expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
    expect(phase()).toBe('pending')
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.pending)
    for (const input of screen.getAllByRole('textbox')) expect(input.matches(':disabled')).toBe(true)
    for (const button of screen.getAllByRole('button')) expect(button.disabled || button.matches(':disabled')).toBe(button.textContent !== 'Close')
    await act(async () => held.resolve(success()))
  })
  it('p19_publication_201', async () => {
    const { p } = await setup()
    await submit(p)
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.published)
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
    // The publication receipt is separate from the subsequently read head.
    const values = within(screen.getByRole('region', { name: 'Tracker layout' })).getByLabelText('Published tracker rows')
    expect([...values.querySelectorAll('dd')].map((x) => x.textContent)).toEqual(['2', '5', '450 W', '0', 'Metres'])
    expect(panel().textContent).toContain(S.manual_scope)
    expect(publish().disabled).toBe(true)
  })
  it('p20_retry_200', async () => {
    const { p } = await setup({ client: { createTrackerRows: vi.fn(async ({ request }) => success(request, 200)) } })
    await submit(p, 'already-published')
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.retry)
    expect(panel().textContent).not.toContain(S.published)
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
  })
  it('p21_result_binding', async () => {
    for (const mutate of [
      (x) => { x.value.drawing_id = 'other' }, (x) => { x.value.project_id = 'other' },
      (x) => { x.value.summary.slots = 6 }, (x) => { x.value.expected_head = H2 },
      (x) => { x.status = 200 }, (x) => { x.value.head.parent = H2 },
      (x) => { x.value = {} }, (x) => { Object.defineProperty(x, 'value', { get() { throw new Error('private') } }) },
    ]) {
      const answer = success(); mutate(answer)
      const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => answer) } })
      await submit(p, 'unknown')
      expect(panel().textContent).toContain(C.TRACKER_ROWS_CLIENT_RESPONSE_INVALID)
      expect(panel().textContent).toContain(S.unknown)
      expect(p.onPhysicalHeadChanged).not.toHaveBeenCalled()
      expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('p22_closed_refusal_copy', async () => {
    for (const [code, text] of [...Object.entries(R), ...Object.entries(C), ['NEW_UNKNOWN_CODE', S.unrecognized]]) {
      const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code,
        message: 'secret server prose', next_action: 'private action', retryable: true })) } })
      fireEvent.click(publish())
      await waitFor(() => expect(screen.getByRole('alert').textContent).toBe(text))
      expect(panel().textContent).not.toContain('secret server prose')
      expect(panel().textContent).not.toContain('private action')
      if (code === 'NEW_UNKNOWN_CODE') expect(panel().textContent).not.toContain(code)
      expect(screen.getByLabelText('Module power').value).toBe('450')
      expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
      cleanup()
    }
    // One panel mount per refusal code, about fifty of them: about 5 s in jsdom, measured.
  }, 60_000)
  it('p23_stale_head_refresh', async () => {
    const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_STALE_HEAD' })) } })
    p.terrainClient.getTerrain.mockResolvedValue(stored(head(H2, 1, H)))
    await submit(p, 'refused')
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(publish().disabled).toBe(false))
    expect(screen.getByRole('alert').textContent).toBe(R.TRACKER_ROWS_STALE_HEAD)
    fireEvent.click(publish())
    await waitFor(() => expect(p.client.createTrackerRows).toHaveBeenCalledTimes(2))
    expect(p.client.createTrackerRows.mock.calls[1][0].request.expected_head).toBe(H2)
  })
  it('p24_terminal_refusals', async () => {
    for (const code of ['TRACKER_ROWS_ALREADY_EXISTS', 'TRACKER_ROWS_GRAPH_CONVERTED']) {
      const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code })) } })
      await submit(p, 'refused')
      change(screen.getByLabelText('Module power'), '451')
      fireEvent.click(refresh())
      await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(refresh().disabled).toBe(false))
      expect(publish().disabled).toBe(true)
      expect(screen.getByRole('alert').textContent).toBe(R[code])
      fireEvent.click(publish())
      expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('p25_retryable_refusal', async () => {
    const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false,
      code: 'TRACKER_ROWS_WRITES_DRAINED', retryable: true })) } })
    await submit(p, 'refused')
    expect(screen.getByRole('alert').textContent).toBe(R.TRACKER_ROWS_WRITES_DRAINED)
    expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
    expect(publish().disabled).toBe(false)
    fireEvent.click(publish())
    await waitFor(() => expect(p.client.createTrackerRows).toHaveBeenCalledTimes(2))
  })
  it('p26_outcome_unknown', async () => {
    for (const code of ['TRACKER_ROWS_CLIENT_TIMEOUT', 'TRACKER_ROWS_CLIENT_NETWORK',
      'TRACKER_ROWS_CLIENT_ABORTED', 'TRACKER_ROWS_CLIENT_RESPONSE_INVALID', 'throw', 'reject']) {
      const createTrackerRows = vi.fn(() => {
        if (code === 'throw') throw new Error('private')
        if (code === 'reject') return Promise.reject(new Error('private'))
        return Promise.resolve({ ok: false, code })
      })
      const { p } = await setup({ client: { createTrackerRows } })
      await submit(p, 'unknown')
      expect(panel().textContent).toContain(C[code] ?? C.TRACKER_ROWS_CLIENT_RESPONSE_INVALID)
      expect(panel().textContent).toContain(S.unknown)
      expect(publish().disabled).toBe(true)
      expect(retry().disabled).toBe(true)
      expect(refresh().disabled).toBe(false)
      expect(screen.getByLabelText('Module power').matches(':disabled')).toBe(true)
      expect(screen.getByLabelText('Module power').value).toBe('450')
      expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('p27_unknown_refresh_only', async () => {
    for (const refreshed of [absent(), stored(head())]) {
      const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_NETWORK' })) } })
      await submit(p, 'unknown')
      const before = structuredClone(p.client.createTrackerRows.mock.calls[0][0].request)
      p.terrainClient.getTerrain.mockResolvedValue(refreshed)
      fireEvent.click(refresh())
      await waitFor(() => expect(phase()).toBe('retry-ready'))
      expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2)
      expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
      expect(p.client.createTrackerRows.mock.calls[0][0].request).toEqual(before)
      expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.retry_ready)
      expect(retry().disabled).toBe(false)
      expect(publish().disabled).toBe(true)
      cleanup()
    }
  })
  it('p28_explicit_original_retry', async () => {
    const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_TIMEOUT' })) } })
    await submit(p, 'unknown')
    const first = p.client.createTrackerRows.mock.calls[0][0]
    p.terrainClient.getTerrain.mockResolvedValue(stored(head()))
    fireEvent.click(refresh())
    await waitFor(() => expect(phase()).toBe('retry-ready'))
    p.client.createTrackerRows.mockImplementation(async ({ request }) => success(request, 200))
    fireEvent.click(retry())
    await waitFor(() => expect(phase()).toBe('already-published'))
    const second = p.client.createTrackerRows.mock.calls[1][0]
    expect(second.request).toEqual(first.request)
    expect(second.request.expected_head).toBeNull()
    expect(second.signal).not.toBe(first.signal)
    expect(p.client.createTrackerRows).toHaveBeenCalledTimes(2)
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
  })
  it('p33_retry_resends_the_original_body', async () => {
    let first
    const client = { createTrackerRows: vi.fn(async ({ request }) => {
      first = JSON.parse(JSON.stringify(request))
      request.expected_head = H2
      request.rows.length = 0
      return { ok: false, code: 'TRACKER_ROWS_CLIENT_TIMEOUT' }
    }) }
    const { p } = await setup({ client })
    await submit(p, 'unknown')
    expect(first).toEqual(m1())
    p.terrainClient.getTerrain.mockResolvedValue(stored(head()))
    fireEvent.click(refresh())
    await waitFor(() => expect(phase()).toBe('retry-ready'))
    p.client.createTrackerRows.mockImplementation(async ({ request }) => success(request, 200))
    fireEvent.click(retry())
    await waitFor(() => expect(phase()).toBe('already-published'))
    const second = p.client.createTrackerRows.mock.calls[1][0]
    expect(second.request).toEqual(m1())
    expect(second.request.expected_head).toBeNull()
  })
  it('p34_unreadable_project_id_blocks_publish', async () => {
    for (const id of ['\ud800', 'x'.repeat(101), 'x'.repeat(201)]) {
      const value = intake(); value.intake.solar_design_graph.project.id = id
      const p = props({ projectId: null, readIntake: vi.fn(async () => value) })
      render(<SolarTrackerRowsPanel {...p} />)
      await waitFor(() => expect(phase()).toBe('read-refused'))
      expect(panel().textContent).toContain(R.TRACKER_ROWS_PROJECT_ID_INVALID)
      expect(publish().disabled).toBe(true)
      expect(p.client.createTrackerRows).not.toHaveBeenCalled()
      cleanup()
    }
    const value = intake(); value.intake.solar_design_graph.project.id = 'x'.repeat(100)
    const { p } = await setup({ projectId: null, readIntake: vi.fn(async () => value) })
    expect(phase()).toBe('ready')
    expect(publish().disabled).toBe(false)
    expect(p.client.createTrackerRows).not.toHaveBeenCalled()
  })
  it('p29_unknown_superseded', async () => {
    for (const [value, text] of [[stored(head(H2, 2, H)), R.TRACKER_ROWS_STALE_HEAD],
      [stored(head(), 'ft'), R.TRACKER_ROWS_UNITS_MISMATCH]]) {
      const { p } = await setup({ client: { createTrackerRows: vi.fn(async () => ({ ok: false, code: 'TRACKER_ROWS_CLIENT_NETWORK' })) } })
      await submit(p, 'unknown')
      p.terrainClient.getTerrain.mockResolvedValue(value)
      fireEvent.click(refresh())
      await waitFor(() => expect(phase()).toBe('superseded'))
      expect(panel().textContent).toContain(text)
      expect(publish().disabled).toBe(true)
      expect(retry().disabled).toBe(true)
      expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
      cleanup()
    }
  })
  it('p30_newest_read_and_signal', async () => {
    const slow = deferred()
    const p = props({ terrainClient: { getTerrain: vi.fn().mockReturnValueOnce(slow.promise).mockResolvedValue(stored(head(H2, 1, H))) } })
    const view = render(<SolarTrackerRowsPanel {...p} headSignal="one" />)
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1))
    const firstSignal = p.terrainClient.getTerrain.mock.calls[0][0].signal
    view.rerender(<SolarTrackerRowsPanel {...p} headSignal="two" />)
    await ready()
    expect(firstSignal.aborted).toBe(true)
    await act(async () => slow.resolve(stored(head())))
    expect(panel().getAttribute('data-head')).toBe(H2)
    view.rerender(<SolarTrackerRowsPanel {...p} headSignal="two" />)
    expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2)
  })
  it('p31_scope_visibility_lifecycle', async () => {
    const held = deferred()
    const { p, view } = await setup({ client: { createTrackerRows: vi.fn(() => held.promise) } })
    fireEvent.click(publish())
    const signal = p.client.createTrackerRows.mock.calls[0][0].signal
    view.rerender(<SolarTrackerRowsPanel {...p} active={false} />)
    expect(screen.queryByRole('region', { name: 'Tracker layout' })).toBeNull()
    expect(signal.aborted).toBe(false)
    await act(async () => held.resolve(success()))
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(1)
    view.rerender(<SolarTrackerRowsPanel {...p} />)
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(2))
    expect(phase()).toBe('published')
    expect(screen.getByLabelText('Module power').value).toBe('450')
    view.rerender(<SolarTrackerRowsPanel {...p} projectId="other" />)
    expect(screen.getByLabelText('Module power').value).toBe('')
    view.unmount()
    cleanup()
    for (const end of ['scope', 'unmount']) {
      const late = deferred()
      const test = await setup({ client: { createTrackerRows: vi.fn(() => late.promise) } })
      fireEvent.click(publish())
      const sent = test.p.client.createTrackerRows.mock.calls[0][0]
      if (end === 'scope') test.view.rerender(<SolarTrackerRowsPanel {...test.p} drawingId="other" />)
      else test.view.unmount()
      expect(sent.signal.aborted).toBe(true)
      await act(async () => late.resolve(success()))
      expect(test.p.onPhysicalHeadChanged).not.toHaveBeenCalled()
      cleanup()
    }
  })
  it('p32_receipt_refresh_and_focus', async () => {
    const held = deferred()
    const { p } = await setup({ client: { createTrackerRows: vi.fn(() => held.promise) },
      onPhysicalHeadChanged: vi.fn(() => { throw new Error('private') }) })
    p.terrainClient.getTerrain.mockResolvedValue({ ok: false })
    publish().focus()
    fireEvent.click(publish())
    screen.getByRole('button', { name: 'Close' }).focus()
    await act(async () => held.resolve(success()))
    await waitFor(() => expect(panel().textContent).toContain(S.read_failed))
    expect(phase()).toBe('published')
    expect(screen.getByTestId('solar-tracker-rows-status').textContent).toBe(S.published)
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close' }))
    expect(screen.getByLabelText('Published tracker rows').textContent).toContain('450 W')
    expect(p.client.createTrackerRows).toHaveBeenCalledTimes(1)
    expect(p.onPhysicalHeadChanged).toHaveBeenCalledTimes(1)
    fireEvent.click(refresh())
    await waitFor(() => expect(p.terrainClient.getTerrain).toHaveBeenCalledTimes(3))
    expect(phase()).toBe('published')
  })
})
