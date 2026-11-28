import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ReportComparison from './ReportComparison.jsx'

const projectId = 'project-one'
const verified = 'Both files match saved Count jobs for this project.'
const unverified = /references come from the files and are unverified/

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function countJob(jobId, { version = 'v1', rule = 'b', roofCount = 1 } = {}) {
  return {
    job_id: jobId, input_version_id: version, status: 'succeeded',
    tool_name: 'count-modules', kind: 'run', project_id: projectId, org_id: 'org-one',
    result: {
      ok: true, tool: 'count-modules', version: 'v1',
      project_context: {
        project_id: projectId, organization_id: 'org-one', drawing_id: 'site-one',
        input_version_id: version, intake_sha256: (version === 'v1' ? 'a' : 'f').repeat(64),
      },
      rule_reference: { schema: 'leaf.count-rule.v1', entry_sha256: rule.repeat(64), geometry_sha256: 'c'.repeat(64) },
      result: {
        selection_required: false, count: roofCount + 1,
        selected_sets: [{ set_id: 'roof', count: roofCount }, { set_id: 'canopy', count: 1 }],
      },
      overlay: { highlight_handles: roofCount === 1 ? ['A', 'B'] : ['A', 'B', 'C'] },
    },
  }
}

async function download(button) {
  const capture = vi.fn(() => 'blob:count-selection')
  const revoke = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = capture
    static revokeObjectURL = revoke
  })
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  try {
    fireEvent.click(screen.getByRole('button', { name: button }))
    await vi.waitFor(() => expect(revoke).toHaveBeenCalledWith('blob:count-selection'))
    return await new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(JSON.parse(reader.result))
      reader.onerror = reject
      reader.readAsText(capture.mock.calls[0][0])
    })
  } finally {
    click.mockRestore()
    vi.unstubAllGlobals()
  }
}

async function exportSelection(job) {
  fireEvent.change(screen.getByRole('combobox', { name: 'Selection to export' }), { target: { value: job.job_id } })
  return download('Download Count selection')
}

function importSelection(label, value) {
  const file = new File([JSON.stringify(value)], 'count-selection.json', { type: 'application/json' })
  fireEvent.change(screen.getByLabelText(label), { target: { files: [file] } })
}

async function compareFiles(first, second, edit = () => {}, error) {
  const view = render(<ReportComparison jobs={[first, second]} projectId={projectId} />)
  fireEvent.click(screen.getByText('Export or compare Count selections'))
  const original = await exportSelection(first)
  const changed = await exportSelection(second)
  edit(changed)
  importSelection('First selection file', original)
  importSelection('Second selection file', changed)
  if (error) await screen.findByText(error)
  else await screen.findByRole('region', { name: 'Exported selection comparison' })
  return view
}

it.each([
  ['rule only', { rule: 'd' }, true, false, []],
  ['drawing only', { version: 'v2', roofCount: 2 }, false, true, ['roof', 'C', null]],
  ['rule and drawing', { version: 'v2', rule: 'd', roofCount: 2 }, true, true, ['roof', 'C', null]],
])('reconciles %s against the authoritative jobs', async (_name, options, ruleChanged, revisionChanged, ids) => {
  const first = countJob('first')
  const second = countJob('second', options)
  await compareFiles(first, second, file => {
    file.selected_sets.reverse()
    file.highlight_handles.reverse()
  })

  expect(screen.getByText(verified)).toBeVisible()
  const comparison = await download('Download selection comparison')
  expect(comparison).toMatchObject({
    source: 'verified_saved_jobs', rule_changed: ruleChanged, drawing_revision_changed: revisionChanged,
  })
  expect(comparison.selections[1].selected_sets).toEqual(second.result.result.selected_sets)
  expect(comparison.selections[1].highlight_handles).toEqual(second.result.overlay.highlight_handles)
  expect(comparison.changes.map(change => change.id)).toEqual(ids)
})

it.each([
  ['unknown job', file => { file.job_id = 'unknown' }],
  ['wrong organization', file => { file.organization_id = 'org-other' }, /same drawing/],
  ['changed drawing fingerprint', file => { file.drawing_revision.intake_sha256 = '1'.repeat(64) }],
  ['changed rule hash', file => { file.rule.geometry_sha256 = '1'.repeat(64) }],
  ['changed set count', file => { file.selected_sets[0].count = 9 }],
  ['changed handle', file => { file.highlight_handles[0] = 'Z' }],
  ['changed total', file => { file.count = 9 }],
])('keeps the %s case unverified', async (_name, edit, error) => {
  const first = countJob('first')
  const second = countJob('second', { version: 'v2' })
  await compareFiles(first, second, edit, error)
  expect(screen.getByText(unverified)).toBeVisible()
  if (error) expect(screen.queryByRole('button', { name: 'Download selection comparison' })).toBeNull()
  else expect(await download('Download selection comparison')).toMatchObject({ source: 'unverified_files' })
})

it('updates verification as saved jobs become ambiguous, disappear and return', async () => {
  const first = countJob('first')
  const second = countJob('second', { version: 'v2' })
  const { rerender } = await compareFiles(first, second)
  expect(screen.getByText(verified)).toBeVisible()

  rerender(<ReportComparison jobs={[first, second, { ...second }]} projectId={projectId} />)
  expect(screen.getByText(unverified)).toBeVisible()
  rerender(<ReportComparison jobs={[]} projectId={projectId} />)
  expect(screen.getByText(unverified)).toBeVisible()
  expect(await download('Download selection comparison')).toMatchObject({ source: 'unverified_files' })
  rerender(<ReportComparison jobs={[first, second]} projectId={projectId} />)
  expect(screen.getByText(verified)).toBeVisible()
})
