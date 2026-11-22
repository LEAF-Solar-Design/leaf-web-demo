import { Blob } from 'node:buffer'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SavedDatasetViewExport } from './SavedDatasetViewExport.jsx'

const reference = { id: 'capacity', version: 1, digest: 'a'.repeat(64) }
const material = { kind: 'project_material', material_id: '11111111-1111-4111-8111-111111111111',
  sha256: 'b'.repeat(64), byte_length: 234 }
const element = { kind: 'dataset.table', id: 'quota-table', dataset_ref: reference, title: 'Quotas',
  query: { fields: ['quota'], limit: 100 }, props: { columns: ['quota'] } }
const record = { revision: 3, element }
const dataset = { reference, revision: 2, material_lineage: material, material_context: { envelope: 'private source bytes' } }
const preferences = { timezone: 'UTC', locale: 'en-US', hourFormat: '24' }
const read = { dataset_ref: reference, status: 'ready', fields: [{ id: 'quota' }],
  rows: [{ key: 'one', values: { quota: 75 } }], next_cursor: null,
  query_coverage: { kind: 'complete_materialization', matching_rows: 1, offset: 0 } }
const props = { projectId: 'project', record, dataset, read, preferences, guard: async () => {}, scopeKey: 'session-a' }
const create = vi.fn(() => 'blob:local')
const revoke = vi.fn()
let click
beforeEach(() => {
  vi.stubGlobal('Blob', Blob)
  vi.stubGlobal('URL', class extends URL { static createObjectURL = create; static revokeObjectURL = revoke })
  click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.clearAllMocks() })

it.each([1, 0])('downloads the saved revision and its complete query of %i records', async count => {
  const result = count ? read : { ...read, status: 'empty', rows: [], query_coverage: { ...read.query_coverage, matching_rows: 0 } }
  render(<SavedDatasetViewExport {...props} read={result} />)
  fireEvent.click(screen.getByRole('button'))
  await waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:local'))
  expect(click.mock.instances[0].download).toBe('quota-table-r3.json')
  expect(JSON.parse(await create.mock.calls[0][0].text())).toEqual({
    schema: 'leaf.saved-dataset-view.v1', project_id: 'project', view: record,
    dataset: { reference, revision: 2, material_lineage: material }, query_result: result, display_preferences: preferences,
  })
})

it.each([{ status: 'stale' }, { next_cursor: 'page2' }, { query_coverage: undefined },
  { dataset_ref: { ...reference, version: 2 } }])('withholds incomplete or mismatched reads', changes => {
  render(<SavedDatasetViewExport {...props} read={{ ...read, ...changes }} />)
  expect(screen.getByRole('button').disabled).toBe(true)
  expect(create).toHaveBeenCalledTimes(0)
})

it('aborts a pending download when the scope changes and coalesces repeated clicks', async () => {
  let finish
  const guard = vi.fn(() => new Promise(resolve => { finish = resolve }))
  const ui = render(<SavedDatasetViewExport {...props} guard={guard} />)
  fireEvent.click(screen.getByRole('button'))
  fireEvent.click(screen.getByRole('button'))
  ui.rerender(<SavedDatasetViewExport {...props} guard={guard} scopeKey="session-b" />)
  await act(async () => finish())
  expect(guard).toHaveBeenCalledTimes(1)
  expect(guard.mock.calls[0][0].aborted).toBe(true)
  expect(create).toHaveBeenCalledTimes(0)
})

it('reports a failed access check without creating a download', async () => {
  render(<SavedDatasetViewExport {...props} guard={async () => { throw Error('denied') }} />)
  fireEvent.click(screen.getByRole('button'))
  expect(await screen.findByRole('alert')).toBeTruthy()
  expect(create).toHaveBeenCalledTimes(0)
})
