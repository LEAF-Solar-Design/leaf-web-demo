import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ProjectLogRetentionPanel } from './ProjectLogRetentionPanel.jsx'

const projectId = '11111111-1111-4111-8111-111111111111'
const execution = `retention-execution:${'a'.repeat(64)}`, table = `retention-table:${'b'.repeat(64)}`
const deletionReview = `retention-review:${'c'.repeat(64)}`, restoreReview = `retention-review:${'d'.repeat(64)}`
const definition = { resourceId: 'retention-definition:logs:v1', revision: 1, definition: {
  kind: 'project_log_retention_definition_v1', external_activation: false, default_mode: 'dry_run', title: 'Synthetic log retention', settings: { batchSize: 100, maxBatchesPerClass: 2 } } }
const dataset = { reference: { id: 'original-logs', version: 1, digest: 'e'.repeat(64) }, revision: 7, title: '<img src=x>',
  material_context: { material_id: 'original-material' }, fields: [{ id: 'record_id', label: 'Record ID' }, { id: 'origin', label: 'Origin' }, { id: 'severity', label: 'Severity' }, { id: 'recorded_at', label: 'Recorded at' }] }
const audit = { resourceId: execution, revision: 1, intent: { schedule: { resourceId: 'retention-schedule:daily:v1', revision: 1 }, definition: { resourceId: definition.resourceId, revision: 1 }, prepared: { resourceId: table, revision: 1 } },
  admittedAt: '2031-01-01T09:45:00.000Z', parentDeadlineAt: '2031-01-01T09:49:00.000Z', expired: false,
  checkpoint: { phase: 'admitted', batches: 0 }, counters: { input: 152, active: 152, archived: 0, deletedBuild: 0, deletedOther: 0, restored: 0, batchesBuild: 0, batchesOther: 0 }, archiveDigest: 'f'.repeat(64) }
const plan = { mode: 'dry_run', expired: false, active_count: 152, retained_count: 2, retentionClock: '2031-01-01T00:00:00.000Z', settings: { batchSize: 100, maxBatchesPerClass: 2 },
  build: { eligible_count: 150, retentionCutoff: '2030-12-18T00:00:00.000Z', archive_count: 151 }, other: { eligible_count: 0, retentionCutoff: '2030-10-03T00:00:00.000Z', archive_count: 0 } }
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
function setup() {
  const controller = new AbortController()
  const client = {
    retentionRegisterDefinition: vi.fn().mockResolvedValue(definition), retentionGetDefinition: vi.fn().mockResolvedValue(definition),
    retentionExportDefinition: vi.fn().mockResolvedValue({ schema_version: 1, kind: 'project_resource', definition: { id: 'logs', version: 1, payload: definition.definition } }),
    listDatasets: vi.fn().mockResolvedValue({ records: [dataset], nextCursor: null }), retentionPrepare: vi.fn().mockResolvedValue({ tableId: table, revision: 1 }),
    retentionRegisterSchedule: vi.fn().mockResolvedValue({ resourceId: 'retention-schedule:daily:v1', revision: 1 }),
    retentionAdmit: vi.fn().mockResolvedValue(audit), retentionAudit: vi.fn().mockResolvedValue(audit),
    retentionRows: vi.fn().mockImplementation(async (_project, id, options) => ({ resourceId: id, revision: 1, view: options.view, total: 0, items: [], nextCursor: null })),
    retentionPlan: vi.fn().mockResolvedValue(plan), retentionReview: vi.fn().mockImplementation(async (_project, _id, input) => ({ reviewId: input.mode === 'restore' ? restoreReview : deletionReview, mode: input.mode, archive_count: 151, archive_digest: 'f'.repeat(64) })),
    retentionApply: vi.fn().mockResolvedValue({ changed_count: 0, stop_reason: 'complete' }),
    retentionAdmitRestore: vi.fn().mockResolvedValue({ authorityId: `retention-execution:${'e'.repeat(64)}`, targetId: execution }),
    retentionRestore: vi.fn().mockResolvedValue({ changed_count: 150, revision: 4 }),
    retentionRecover: vi.fn().mockResolvedValue({ changed_count: 0, revision: 4, stop_reason: 'paused' }),
  }
  const props = { client, projectId, signal: controller.signal, guard: vi.fn().mockResolvedValue(), onForbidden: vi.fn() }
  return { props, client, controller }
}
const click = name => fireEvent.click(screen.getByRole('button', { name }))
const change = (label, value) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const idle = async () => waitFor(() => expect(screen.getByRole('button', { name: 'Save definition' })).toBeEnabled())
async function saveDefinition() { change('Definition slug', 'logs'); change('Definition title', 'Synthetic log retention'); change('Batch size', '100'); change('Maximum batches per class', '2'); click('Save definition'); await idle() }
async function openRun() { change('Execution ID', execution); click('Open run'); await screen.findByRole('region', { name: 'Actual run audit' }); await screen.findByText('No records in this view.') }
async function previewAndReview() { click('Preview retention'); await screen.findByRole('region', { name: 'Retention preview' }); click('Review deletion'); await idle() }
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('O03 standalone retention controls, inert client only', () => {
  it('saves editable immutable definition, reopens actual settings and exports actual payload', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await saveDefinition()
    expect(h.client.retentionRegisterDefinition).toHaveBeenCalledWith(projectId, { id: 'logs', version: 1, title: 'Synthetic log retention', settings: { batchSize: 100, maxBatchesPerClass: 2 } }, expect.objectContaining({ signal: expect.any(AbortSignal) }))
    click('Open definition'); await waitFor(() => expect(h.client.retentionGetDefinition).toHaveBeenCalled())
    click('Export definition'); expect(await screen.findByText('Exported definition')).toBeInTheDocument()
    expect(screen.getByText(/"schema_version": 1/)).toBeInTheDocument()
  })
  it('prepares only actual imported ref/revision and explicit field mapping/UTC clock', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets')
    await screen.findByRole('option', { name: '<img src=x>' }); change('Imported dataset', JSON.stringify([dataset.reference, dataset.revision]))
    change('Logical table ID', 'my-original-logs'); change('Retention clock (UTC milliseconds)', '2031-01-01T00:00:00.000Z')
    change('Record ID field', 'record_id'); change('Source field', 'origin'); change('Level field', 'severity'); change('Timestamp field', 'recorded_at')
    click('Prepare records'); await idle()
    expect(h.client.retentionPrepare).toHaveBeenCalledWith(projectId, { datasetRef: dataset.reference, expectedDatasetRevision: 7, tableId: 'my-original-logs', retentionClock: '2031-01-01T00:00:00.000Z', fields: { id: 'record_id', source: 'origin', level: 'severity', timestamp: 'recorded_at' } }, expect.any(Object))
    expect(screen.getByText(`Prepared table: ${table}.`)).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })
  it('downloads only actual exported definition and redacted audit objects and revokes object URLs', async () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:synthetic'), revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await saveDefinition()
    click('Export definition'); await screen.findByRole('button', { name: 'Download definition' }); click('Download definition'); await idle()
    await openRun(); click('Export audit'); await screen.findByRole('button', { name: 'Download audit' }); click('Download audit'); await idle()
    expect(createObjectURL).toHaveBeenCalledTimes(2); expect(anchorClick).toHaveBeenCalledTimes(2); expect(revokeObjectURL).toHaveBeenCalledTimes(2)
    const text = blob => new Promise(resolve => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.readAsText(blob) })
    expect(JSON.parse(await text(createObjectURL.mock.calls[0][0]))).toEqual(await h.client.retentionExportDefinition.mock.results[0].value)
    expect(JSON.parse(await text(createObjectURL.mock.calls[1][0]))).toEqual(audit)
    expect(document.querySelector('a[download]')).toBeNull()
  })
  it('does not download an already displayed export after guard revocation', async () => {
    const createObjectURL = vi.fn(), revokeObjectURL = vi.fn(); vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await saveDefinition()
    click('Export definition'); await screen.findByRole('button', { name: 'Download definition' })
    h.props.guard.mockRejectedValue(Object.assign(new Error('denied'), { status: 403 })); click('Download definition')
    await screen.findByRole('alert'); expect(createObjectURL).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Download definition' })).not.toBeInTheDocument()
  })
  it('does not enable preparation for a dataset without imported material context', async () => {
    const h = setup(); h.client.listDatasets.mockResolvedValue({ records: [{ ...dataset, material_context: undefined }], nextCursor: null })
    render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets'); await screen.findByRole('option', { name: '<img src=x>' })
    expect(screen.getByRole('button', { name: 'Prepare records' })).toBeDisabled()
  })
  it('saves fixed internal schedule against actual resources and admits explicit canonical due date', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await saveDefinition()
    click('Load datasets'); await screen.findByRole('option', { name: '<img src=x>' }); change('Imported dataset', JSON.stringify([dataset.reference, 7]))
    change('Logical table ID', 'logs-table'); change('Retention clock (UTC milliseconds)', '2031-01-01T00:00:00.000Z'); click('Prepare records'); await idle()
    change('Schedule slug', 'daily'); click('Save internal schedule'); await idle()
    expect(h.client.retentionRegisterSchedule).toHaveBeenCalledWith(projectId, { id: 'daily', version: 1, definition: { resourceId: definition.resourceId, revision: 1 }, preparedTableId: table }, expect.any(Object))
    change('Occurrence due date', '2031-01-02'); click('Admit internal run'); await idle()
    expect(h.client.retentionAdmit).toHaveBeenCalledWith(projectId, expect.objectContaining({ dueAt: '2031-01-02T09:45:00.000Z', preparedTableId: table }), expect.any(Object))
    expect(screen.getByText(/External activation is disabled/)).toBeInTheDocument()
  })
  it('preview dispatches no review, apply or restore writes', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await openRun()
    click('Preview retention'); await screen.findByRole('region', { name: 'Retention preview' })
    expect(h.client.retentionPlan).toHaveBeenCalledWith(projectId, execution, {}, expect.any(Object))
    expect(h.client.retentionReview).not.toHaveBeenCalled(); expect(h.client.retentionApply).not.toHaveBeenCalled(); expect(h.client.retentionRestore).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Apply reviewed retention' })).toBeDisabled()
  })
  it('uses the same explicit review across bounded acknowledged batches and refreshes actual receipts', async () => {
    const h = setup(); h.client.retentionApply.mockResolvedValueOnce({ changed_count: 100, stop_reason: 'paused' }).mockResolvedValueOnce({ changed_count: 50, stop_reason: 'complete' }).mockResolvedValue({ changed_count: 0, stop_reason: 'complete' })
    render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); await previewAndReview(); click('Apply reviewed retention'); await idle()
    expect(h.client.retentionApply.mock.calls.map(call => call[2])).toEqual([{ reviewId: deletionReview, class: 'build' }, { reviewId: deletionReview, class: 'build' }])
    expect(h.client.retentionReview).toHaveBeenCalledTimes(1); expect(h.client.retentionAudit.mock.calls.length).toBeGreaterThan(1)
  })
  it('stops only after current batch receipt without aborting or retrying it', async () => {
    const h = setup(), held = deferred(); h.client.retentionApply.mockReturnValueOnce(held.promise)
    render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); await previewAndReview(); click('Apply reviewed retention')
    await waitFor(() => expect(h.client.retentionApply).toHaveBeenCalledTimes(1)); click('Stop after current batch')
    expect(h.client.retentionApply.mock.calls[0][3].signal.aborted).toBe(false)
    held.resolve({ changed_count: 100, stop_reason: 'paused' }); await idle()
    expect(h.client.retentionApply).toHaveBeenCalledTimes(1); expect(screen.getByText('Stopped after the last acknowledged batch.')).toBeInTheDocument()
  })
  it('continues the other class when a resumed build class already reached its batch cap', async () => {
    const h = setup()
    h.client.retentionAudit.mockResolvedValue({ ...audit, counters: { ...audit.counters, input: 272, active: 172,
      archived: 271, deletedBuild: 100, batchesBuild: 1 } })
    h.client.retentionPlan.mockResolvedValue({ ...plan, active_count: 172, retained_count: 2,
      settings: { batchSize: 100, maxBatchesPerClass: 1 }, build: { ...plan.build, eligible_count: 50 },
      other: { ...plan.other, eligible_count: 120, archive_count: 120 } })
    h.client.retentionApply.mockResolvedValueOnce({ changed_count: 0, stop_reason: 'max_batches' })
      .mockResolvedValueOnce({ changed_count: 100, revision: 7, stop_reason: 'max_batches' })
    render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); await previewAndReview(); click('Apply reviewed retention'); await idle()
    expect(h.client.retentionApply.mock.calls.map(call => call[2])).toEqual([
      { reviewId: deletionReview, class: 'build' }, { reviewId: deletionReview, class: 'other' },
    ])
    expect(h.client.retentionReview).toHaveBeenCalledTimes(1)
  })
  it('clears review and never retries an uncertain dispatched apply failure', async () => {
    const h = setup(); h.client.retentionApply.mockRejectedValue(Object.assign(new Error('private'), { dispatched: true }))
    render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); await previewAndReview(); click('Apply reviewed retention'); await idle()
    expect(h.client.retentionApply).toHaveBeenCalledTimes(1); expect(screen.getByRole('alert')).toHaveTextContent('Refresh run')
    expect(screen.getByRole('button', { name: 'Apply reviewed retention' })).toBeDisabled(); expect(screen.getByRole('alert')).not.toHaveTextContent('private')
  })
  it('manual restore uses one generated invocation and actual authority while preserving original run ID', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); click('Review restore'); await idle(); click('Restore archived records'); await idle()
    expect(h.client.retentionAdmitRestore).toHaveBeenCalledWith(projectId, execution, { reviewId: restoreReview, invocationId: expect.stringMatching(/^[a-f0-9]{64}$/) }, expect.any(Object))
    expect(h.client.retentionRestore).toHaveBeenCalledWith(projectId, execution, { authorityId: `retention-execution:${'e'.repeat(64)}` }, expect.any(Object))
    expect(screen.getByLabelText('Execution ID')).toHaveValue(execution)
  })
  it('explicit recovery is metadata-only and never resumes apply', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); click('Recover expired batch metadata'); await idle()
    expect(h.client.retentionRecover).toHaveBeenCalledWith(projectId, execution, expect.any(Object)); expect(h.client.retentionApply).not.toHaveBeenCalled()
  })
  it('pages current/archive records with cursor stack and escapes supplied row values', async () => {
    const h = setup(); h.client.retentionRows.mockImplementation(async (_p, id, options) => ({ resourceId: id, revision: 1, view: options.view, total: 2,
      items: [{ id: options.after ? 'second' : '<script>private</script>' }], nextCursor: options.after ? null : 'next-page' }))
    render(<ProjectLogRetentionPanel {...h.props} />); change('Execution ID', execution); click('Open run'); await screen.findByText('<script>private</script>')
    expect(document.querySelector('script')).toBeNull(); click('Next records'); await screen.findByText('second')
    expect(h.client.retentionRows).toHaveBeenLastCalledWith(projectId, execution, expect.objectContaining({ view: 'current', after: 'next-page', limit: 25 }))
    click('Previous records'); await screen.findByText('<script>private</script>'); click('Archive records')
    await waitFor(() => expect(h.client.retentionRows).toHaveBeenLastCalledWith(projectId, execution, expect.objectContaining({ view: 'archive', limit: 25 })))
  })
  it('failed row read clears previous rows and exposes explicit retry', async () => {
    const h = setup(); render(<ProjectLogRetentionPanel {...h.props} />); await openRun(); h.client.retentionRows.mockRejectedValue(new Error('unreadable'))
    click('Archive records'); await screen.findByRole('alert'); expect(screen.getByRole('button', { name: 'Retry rows' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
  it('sets busy before first await so duplicate save clicks dispatch once', async () => {
    const h = setup(), held = deferred(); h.client.retentionRegisterDefinition.mockReturnValue(held.promise)
    render(<ProjectLogRetentionPanel {...h.props} />); change('Definition slug', 'logs'); change('Definition title', 'Logs')
    click('Save definition'); click('Save definition'); await waitFor(() => expect(h.client.retentionRegisterDefinition).toHaveBeenCalledTimes(1))
    held.resolve(definition); await idle()
  })
  it('checks guard before dispatch and refuses adopting data after guard revocation', async () => {
    const h = setup(); h.props.guard.mockRejectedValue(Object.assign(new Error('denied'), { status: 403 }))
    render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets'); await screen.findByRole('alert')
    expect(h.client.listDatasets).not.toHaveBeenCalled(); expect(h.props.onForbidden).toHaveBeenCalled()
  })
  it('revalidates after a read and never adopts returned rows when access is revoked', async () => {
    const h = setup(); h.props.guard.mockResolvedValueOnce().mockRejectedValue(Object.assign(new Error('revoked'), { status: 403 }))
    render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets'); await screen.findByRole('alert')
    expect(h.client.listDatasets).toHaveBeenCalledTimes(1); expect(h.props.onForbidden).toHaveBeenCalled()
    expect(screen.queryByRole('option', { name: '<img src=x>' })).not.toBeInTheDocument()
  })
  it('unmount aborts a pending read and prevents any late readback adoption', async () => {
    const h = setup(), held = deferred(); h.client.listDatasets.mockReturnValue(held.promise)
    const rendered = render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets')
    await waitFor(() => expect(h.client.listDatasets).toHaveBeenCalledTimes(1)); rendered.unmount()
    expect(h.client.listDatasets.mock.calls[0][1].signal.aborted).toBe(true)
    held.resolve({ records: [dataset], nextCursor: null }); await Promise.resolve()
    expect(screen.queryByRole('region', { name: 'Ops log retention' })).not.toBeInTheDocument()
  })
  it('ignores late readback after signal abort and removes private state', async () => {
    const h = setup(), held = deferred(); h.client.listDatasets.mockReturnValue(held.promise)
    render(<ProjectLogRetentionPanel {...h.props} />); click('Load datasets'); await waitFor(() => expect(h.client.listDatasets).toHaveBeenCalled())
    h.controller.abort(); held.resolve({ records: [dataset], nextCursor: null })
    await screen.findByRole('alert'); expect(screen.queryByRole('option', { name: '<img src=x>' })).not.toBeInTheDocument()
  })
  it('context replacement and unmount retire clients that ignore abort; callback rerenders do not reset state', async () => {
    const h = setup(), held = deferred(); h.client.listDatasets.mockReturnValue(held.promise)
    const rendered = render(<ProjectLogRetentionPanel {...h.props} />); change('Definition title', 'Local title'); rendered.rerender(<ProjectLogRetentionPanel {...h.props} guard={vi.fn().mockResolvedValue()} />)
    expect(screen.getByLabelText('Definition title')).toHaveValue('Local title')
    click('Load datasets'); await waitFor(() => expect(h.client.listDatasets).toHaveBeenCalled())
    rendered.rerender(<ProjectLogRetentionPanel {...h.props} projectId="22222222-2222-4222-8222-222222222222" />)
    expect(screen.getByLabelText('Definition title')).toHaveValue(''); held.resolve({ records: [dataset], nextCursor: null })
    await waitFor(() => expect(screen.queryByRole('option', { name: '<img src=x>' })).not.toBeInTheDocument()); rendered.unmount()
    expect(h.client.listDatasets.mock.calls[0][1].signal.aborted).toBe(true)
  })
})
