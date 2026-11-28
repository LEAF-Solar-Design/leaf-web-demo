import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ReportComparison from './ReportComparison.jsx'

const projectId = 'project-one'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function countJob(jobId, revision, intake, entry, geometry) {
  return {
    job_id: jobId,
    org_id: 'org-one',
    project_id: projectId,
    input_version_id: revision,
    kind: 'run',
    tool_name: 'count-modules',
    status: 'succeeded',
    result: {
      ok: true,
      tool: 'count-modules',
      version: 'v1',
      project_context: {
        project_id: projectId,
        organization_id: 'org-one',
        drawing_id: 'site-a',
        input_version_id: revision,
        intake_sha256: intake.repeat(64),
      },
      rule_reference: {
        schema: 'leaf.count-rule.v1',
        entry_sha256: entry.repeat(64),
        geometry_sha256: geometry.repeat(64),
      },
      result: { selection_required: false, count: 1, selected_sets: [{ set_id: 'roof', count: 1 }] },
      overlay: { highlight_handles: ['A'] },
    },
  }
}

function readText(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = reject
    reader.readAsText(blob)
  })
}

it('explains restored, persistent and revert-only Count input changes with an unchanged selection', async () => {
  const original = countJob('job-original', 'drawing-v1', 'a', 'b', 'c')
  const edited = countJob('job-edited', 'drawing-v1', 'a', 'd', 'e')
  const reverted = countJob('job-reverted', 'drawing-v2', 'f', 'b', 'e')
  const createUrl = vi.fn(() => 'blob:count-selection')
  const revokeUrl = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createUrl
    static revokeObjectURL = revokeUrl
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

  render(<ReportComparison jobs={[original, edited, reverted]} projectId={projectId} />)
  fireEvent.click(screen.getByText('Export or compare Count selections'))
  const exportPicker = screen.getByRole('combobox', { name: 'Selection to export' })
  const exportButton = screen.getByRole('button', { name: 'Download Count selection' })
  for (const [job, fileLabel] of [
    [original, 'First selection file'],
    [edited, 'Second selection file'],
    [reverted, 'Reverted selection file (optional)'],
  ]) {
    fireEvent.change(exportPicker, { target: { value: job.job_id } })
    fireEvent.click(exportButton)
    await vi.waitFor(() => expect(revokeUrl).toHaveBeenCalledTimes(createUrl.mock.calls.length))
    const [blob] = createUrl.mock.lastCall
    const file = new File([blob], 'count-selection.json', { type: blob.type })
    fireEvent.change(screen.getByLabelText(fileLabel), { target: { files: [file] } })
  }

  const history = await screen.findByRole('region', { name: 'Exported selection history' })
  expect(within(history).getByText('Reverted selection matches the original')).toBeVisible()
  expect(within(history).getByText('No saved selection changes')).toBeVisible()
  const edit = within(history).getByRole('group', { name: 'Original to edited' })
  const revert = within(history).getByRole('group', { name: 'Edited to reverted' })
  expect(within(edit).getAllByRole('listitem').map(item => item.textContent)).toEqual([
    'Count rule: Changed', 'Geometry dependency: Changed', 'Drawing revision: Same',
  ])
  expect(within(revert).getAllByRole('listitem').map(item => item.textContent)).toEqual([
    'Count rule: Changed', 'Geometry dependency: Same', 'Drawing revision: Changed',
  ])

  fireEvent.click(within(history).getByRole('button', { name: 'Download selection history' }))
  await vi.waitFor(() => expect(revokeUrl).toHaveBeenCalledTimes(4))
  const [historyBlob] = createUrl.mock.lastCall
  const exported = JSON.parse(await readText(historyBlob))
  expect(exported).toMatchObject({
    schema: 'leaf.saved-selection-history.v1',
    source: 'unverified_files',
    matches_original: true,
    changes: [],
    provenance_changes: {
      original_to_edited: { rule_entry_changed: true, geometry_dependency_changed: true, drawing_revision_changed: false },
      edited_to_reverted: { rule_entry_changed: true, geometry_dependency_changed: false, drawing_revision_changed: true },
    },
  })
})
