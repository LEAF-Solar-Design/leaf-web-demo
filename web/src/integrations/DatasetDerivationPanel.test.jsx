import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { TextEncoder } from 'node:util'
import { DatasetDerivationPanel, DatasetDerivationEvidence } from './DatasetDerivationPanel.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const reference = { id: 'source', version: 1, digest: 'a'.repeat(64) }
const field = (id, kind, nullable = false) => ({ id, label: id, type: { kind }, nullable, sensitivity: 'internal' })
const fields = [field('identity', 'identifier'), field('repo', 'text'), field('at', 'instant'), field('open_count', 'count'), field('missing', 'text', true)]
const dataset = { reference, title: 'Imported observations', fields, material_context: { source: { kind: 'project_material' } } }
const capabilities = { schema_version: 1, contract: 'dataset-derivation-v1', operations: ['group_count', 'window_count'], max_inputs: 2, max_records_per_input: 100000, max_windows: 4 }
const result = { reference: { id: 'counts', version: 1, digest: 'b'.repeat(64) }, revision: 1, rowCount: 2, status: 'complete' }
function setup() {
  const controller = new AbortController()
  const props = { projectId, contextKey: 'scope-one', signal: controller.signal, datasets: [dataset],
    client: { getDatasetDerivationCapabilities: vi.fn().mockResolvedValue(capabilities), createDatasetDerivation: vi.fn().mockResolvedValue(result) },
    guard: vi.fn().mockResolvedValue(undefined), onCreated: vi.fn().mockResolvedValue(undefined), onForbidden: vi.fn() }
  return { props, controller }
}
function change(label, value) { fireEvent.change(screen.getByLabelText(label), { target: { value } }) }
async function fill() {
  await screen.findByLabelText('Source dataset')
  change('Source dataset', JSON.stringify([reference.id, reference.version, reference.digest]))
  change('Output ID', 'counts'); change('Output title', 'Imported counts'); change('Window ID', 'all')
  fireEvent.click(within(screen.getByRole('group', { name: 'Count distinct identities by' })).getByLabelText('identity'))
}
beforeEach(() => vi.stubGlobal('TextEncoder', TextEncoder))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('loads exact capabilities and creates from explicit source choices, then adopts the returned immutable ref', async () => {
  const { props } = setup(); render(<DatasetDerivationPanel {...props} />); await fill()
  fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.onCreated).toHaveBeenCalledWith(result.reference, expect.any(AbortSignal)))
  expect(props.client.createDatasetDerivation).toHaveBeenCalledTimes(1)
  expect(props.client.createDatasetDerivation.mock.calls[0][1]).toEqual({ schema_version: 1, output: { id: 'counts', title: 'Imported counts', version: 1 }, inputs: { primary: reference },
    plan: { kind: 'group_count', filters: [], group_by: [], count: { as: 'row_count', label: 'Count', distinct_by: ['identity'] }, windows: [{ id: 'all', filters: [] }] } })
  expect(screen.getByLabelText('Output title')).toHaveValue('Imported counts')
})

it('builds explicit observation windows, partitions and quotient reasons without replacing source clocks', async () => {
  const { props } = setup(); render(<DatasetDerivationPanel {...props} />); await fill()
  change('Operation', 'window_count'); change('Event dataset', JSON.stringify([reference.id, reference.version, reference.digest]))
  fireEvent.click(within(screen.getByRole('group', { name: 'Retained observation fields' })).getByLabelText('at'))
  fireEvent.click(within(screen.getByRole('group', { name: 'Retained observation fields' })).getByLabelText('open_count'))
  change('Observation timestamp', 'at'); change('Event timestamp', 'at'); change('Lookback seconds', '86400')
  fireEvent.click(within(screen.getByRole('group', { name: 'Count distinct identities by' })).getByLabelText('identity'))
  fireEvent.click(screen.getByRole('button', { name: 'Add matching partition' }))
  change('Observation partition field', 'repo'); change('Event partition field', 'repo')
  fireEvent.click(screen.getByRole('button', { name: 'Add quotient estimate' }))
  change('Estimate field ID', 'days'); change('Estimate label', 'Estimated days'); change('Reason field ID', 'days_reason'); change('Reason label', 'Estimate reason')
  change('Numerator field', 'open_count'); change('Denominator field', 'row_count'); change('Multiplier', '7')
  fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.onCreated).toHaveBeenCalled())
  expect(props.client.createDatasetDerivation.mock.calls[0][1]).toMatchObject({ plan: { kind: 'window_count', anchor_fields: ['at', 'open_count'], lookback_seconds: 86400,
    lower_inclusive: false, upper_inclusive: true, partition_by: [{ anchor_field: 'repo', event_field: 'repo' }] },
    computed: [{ as: 'days', numerator: 'open_count', denominator: 'row_count', multiplier: 7, reason_as: 'days_reason' }] })
})

it('validates strict window names and nonmissing count identities before a write', async () => {
  const { props } = setup(); render(<DatasetDerivationPanel {...props} />); await fill()
  change('Window ID', 'invalid/id')
  fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Review dataset choices')
  expect(props.client.createDatasetDerivation).not.toHaveBeenCalled()
  expect(within(screen.getByRole('group', { name: 'Count distinct identities by' })).queryByLabelText('missing')).not.toBeInTheDocument()
})

it('does not create when capabilities are refused or structurally invalid', async () => {
  const { props } = setup(); props.client.getDatasetDerivationCapabilities.mockResolvedValue({ ...capabilities, operations: ['sql'] })
  render(<DatasetDerivationPanel {...props} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('unavailable')
  expect(screen.queryByLabelText('Source dataset')).not.toBeInTheDocument()
  expect(props.client.createDatasetDerivation).not.toHaveBeenCalled()
})

it.each([401, 403])('retires denied creation with status %s and never adopts the result', async status => {
  const { props } = setup(); props.client.createDatasetDerivation.mockRejectedValue({ status })
  render(<DatasetDerivationPanel {...props} />); await fill()
  fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledTimes(1))
  expect(props.onCreated).not.toHaveBeenCalled()
})

it('does not adopt a result for a different immutable output', async () => {
  const { props } = setup(); props.client.createDatasetDerivation.mockResolvedValue({ ...result, reference: { ...result.reference, id: 'foreign' } })
  render(<DatasetDerivationPanel {...props} />); await fill(); fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await screen.findByRole('alert'); expect(props.onCreated).not.toHaveBeenCalled()
})

it('keeps successful creation separate from failed adoption and refreshes only the read choice', async () => {
  const { props } = setup(); props.onCreated.mockRejectedValueOnce(new Error('Temporary read failure'))
  render(<DatasetDerivationPanel {...props} />); await fill(); fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('dataset was created')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh created dataset choice' }))
  await waitFor(() => expect(props.onCreated).toHaveBeenCalledTimes(2))
  expect(props.client.createDatasetDerivation).toHaveBeenCalledTimes(1)
})

it('dispatches once while a creation is pending and checks fresh authority before dispatch', async () => {
  const { props } = setup(); let resolve
  props.client.createDatasetDerivation.mockImplementation(() => new Promise(done => { resolve = done }))
  render(<DatasetDerivationPanel {...props} />); await fill()
  fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.client.createDatasetDerivation).toHaveBeenCalledTimes(1))
  expect(screen.getByRole('button', { name: 'Creating derived dataset…' })).toBeDisabled()
  expect(props.guard).toHaveBeenCalledTimes(1)
  await act(async () => resolve(result))
  expect(props.client.createDatasetDerivation).toHaveBeenCalledTimes(1)
})

it('fresh write authority refusal prevents dispatch while retaining the explicit draft', async () => {
  const { props } = setup(); props.guard.mockRejectedValue({ status: 403 })
  render(<DatasetDerivationPanel {...props} />); await fill(); fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.onForbidden).toHaveBeenCalledTimes(1))
  expect(props.client.createDatasetDerivation).not.toHaveBeenCalled()
  expect(screen.getByLabelText('Output title')).toHaveValue('Imported counts')
})

it('ignores a creation response that arrives after context retirement even if client ignores abort', async () => {
  const { props, controller } = setup(); let resolve
  props.client.createDatasetDerivation.mockImplementation(() => new Promise(done => { resolve = done }))
  render(<DatasetDerivationPanel {...props} />); await fill(); fireEvent.click(screen.getByRole('button', { name: 'Create derived dataset' }))
  await waitFor(() => expect(props.client.createDatasetDerivation).toHaveBeenCalled())
  await act(async () => { controller.abort(); resolve(result) })
  expect(props.onCreated).not.toHaveBeenCalled()
})

it('ignores an old project capability response and loads the next project independently', async () => {
  const { props } = setup(); let resolve
  props.client.getDatasetDerivationCapabilities.mockImplementationOnce(() => new Promise(done => { resolve = done }))
  const view = render(<DatasetDerivationPanel {...props} />)
  await waitFor(() => expect(props.client.getDatasetDerivationCapabilities).toHaveBeenCalledTimes(1))
  view.rerender(<DatasetDerivationPanel {...props} projectId="22222222-2222-4222-8222-222222222222" contextKey="scope-two" />)
  await screen.findByLabelText('Source dataset'); await act(async () => resolve({ ...capabilities, operations: ['sql'] }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('shows per-input unknown clocks, explicit diagnostics and honest coverage rather than fabricating age', () => {
  const freshness = { fetched_at: '2026-11-12T12:00:00Z', source_updated_at: null, synced_at: null }
  render(<DatasetDerivationEvidence dataset={{ reference: result.reference, computed_at: '2026-11-12T12:01:00Z', lastAttempt: { row_count: 2 },
    derivation: { plan: { kind: 'group_count' }, inputs: [{ alias: 'primary', dataset_ref: reference, original_binding: { material_id: projectId } }],
      computed: [{ as: 'days', label: 'Estimate', reason_label: 'Estimate reason' }] },
    input_freshness: [{ alias: 'primary', freshness }], diagnostics: { excluded_domain_rows: 4, null_event_time_rows: 3 } }} />)
  expect(screen.getAllByText('Unknown')).toHaveLength(2)
  expect(screen.getByText(/Provider history and missing observations may be incomplete/)).toBeInTheDocument()
  expect(screen.getByText(/Rows outside the declared scope: 4/)).toHaveTextContent('missing timestamps: 3')
  expect(screen.getByText(/missing estimates retain a reason/)).toBeInTheDocument()
})
