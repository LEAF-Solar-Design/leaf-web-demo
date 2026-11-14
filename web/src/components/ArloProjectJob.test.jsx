import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { openProject, getArloReviews, saveArloReview } from '../api.js'
import WorkspaceSummary from './WorkspaceSummary.jsx'
import ArloProjectJob from './ArloProjectJob.jsx'

vi.mock('../api.js', () => ({ openProject:vi.fn(), getArloReviews:vi.fn(), saveArloReview:vi.fn() }))
const org = '11111111-1111-4111-8111-111111111111'
const projectId = '22222222-2222-4222-8222-222222222222'
const jobId = '33333333-3333-4333-8333-333333333333'
const version = '44444444-4444-4444-8444-444444444444'
const project = {org_id:org, project_id:projectId, name:'ARLO lab'}
const proposal = {proposal_id:'p1', production_valid:false, violations:[], routes:[], placements:[], quantities:[], estimated_installed_cost:54}
const job = {job_id:jobId, org_id:org, project_id:projectId, input_version_id:version, tool_name:'arlo-design', status:'succeeded',
  result:{solver:'arlo-design', result_sha256:'a'.repeat(64), solver_input:{organization_id:org,project_id:projectId,input_version_id:version},
    solver_result:{status:'complete',proposals:[proposal],trace:[]}}}
const workspace = {project, jobs:[job], drawing_versions:[], built_tools:[]}
beforeEach(() => {
  openProject.mockResolvedValue(workspace)
  getArloReviews.mockResolvedValue({job_id:jobId,result_sha256:'a'.repeat(64),reviews:[]})
  saveArloReview.mockImplementation(async (_context, body) => ({review:{operation_id:'decision1', created_at:'now',
    payload:{jobId,resultHash:body.result_sha256,proposalId:body.proposal_id,decision:body.decision,note:body.note}}}))
})
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
function captureDownload() {
  const createObjectURL = vi.fn(() => 'blob:saved-arlo'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL
    static revokeObjectURL = revokeObjectURL
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  return { createObjectURL, revokeObjectURL }
}
function readJson(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}

it('exports the canonical reopened result exactly as previewed, including stored limitations and provenance', async () => {
  const captured = captureDownload()
  const freshResult = { ...job.result, input_sha256: 'b'.repeat(64), solver_revision: 'saved-revision',
    solver_input: { ...job.result.solver_input, requirements: { support_spacing: 2 } },
    solver_result: { ...job.result.solver_result, diagnostics: { limitations: ['Native host attachment pending.'] },
      proposals: [{ ...proposal, source: { source_id: 'saved-source' },
        routes: [{ points: [{ x: 0, y: 0, z: 2 }, { x: 3, y: 0, z: 2 }] }],
        cost_breakdown: [{ kind: 'conduit', cost: 54 }] }],
      trace: [{ event: 'candidate_selected', proposal_id: 'p1' }] } }
  openProject.mockResolvedValue({ ...workspace, jobs: [{ ...job, result: freshResult }] })
  render(<WorkspaceSummary workspace={workspace} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  await screen.findByRole('region', { name: 'Saved feeder proposal' })
  const preview = JSON.parse(screen.getByLabelText('Export JSON preview').textContent)
  expect(preview.envelope).toEqual(freshResult)
  expect(preview.envelope).not.toEqual(job.result)
  expect(JSON.parse(screen.getByLabelText('Proposal JSON').textContent)).toEqual(freshResult.solver_result.proposals[0])
  expect(screen.getByRole('region', { name: 'Limitations' })).toHaveTextContent('Native host attachment pending.')
  fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
  expect(await readJson(captured.createObjectURL.mock.calls[0][0])).toEqual(preview)
  await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledWith('blob:saved-arlo'))
  expect(saveArloReview).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Close proposal' }))
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  await screen.findByRole('region', { name: 'Saved feeder proposal' })
  expect(screen.getByRole('button', { name: 'Export result' })).toBeEnabled()
  expect(screen.getByRole('region', { name: 'Limitations' })).toHaveTextContent('Native host attachment pending.')
  fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
  expect(await readJson(captured.createObjectURL.mock.calls[1][0])).toEqual(preview)
  await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledTimes(2))
})

it.each(['incomplete', 'cancelled'])('reopens an empty %s result with its diagnostics and an export equal to preview', async status => {
  const captured = captureDownload()
  const freshResult = { ...job.result, limitations: ['Native family compatibility unverified.'],
    solver_result: { status, production_valid: false, proposals: [],
      limitations: ['No complete candidate was found.'],
      trace: [{ event: 'solve_stopped', reason: status }] } }
  openProject.mockResolvedValue({ ...workspace, jobs: [{ ...job, result: freshResult }] })
  render(<ArloProjectJob project={project} job={job} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  await screen.findByRole('region', { name: 'Saved feeder proposal' })
  expect(screen.getByText('Feeder design incomplete')).toBeVisible()
  expect(screen.getByRole('region', { name: 'Limitations' })).toHaveTextContent('No complete candidate was found.')
  expect(screen.getByRole('region', { name: 'Limitations' })).toHaveTextContent('Native family compatibility unverified.')
  expect(screen.getByText('Trace event count: 1')).toBeVisible()
  expect(JSON.parse(screen.getByLabelText('Trace events JSON').textContent)).toEqual(freshResult.solver_result.trace)
  fireEvent.click(screen.getByText('Inspect full stored result and export JSON'))
  const preview = JSON.parse(screen.getByLabelText('Export JSON preview').textContent)
  expect(preview.envelope).toEqual(freshResult)
  expect(screen.getByRole('button', { name: 'Export result' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
  expect(await readJson(captured.createObjectURL.mock.calls[0][0])).toEqual(preview)
  await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledWith('blob:saved-arlo'))
  expect(saveArloReview).not.toHaveBeenCalled()
})

it.each(['project', 'organization', 'job', 'input version'])('disables an opened export when the current %s changes', async scope => {
  const captured = captureDownload()
  const { rerender } = render(<ArloProjectJob project={project} job={job} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  await screen.findByRole('region', { name: 'Saved feeder proposal' })
  expect(screen.getByRole('button', { name: 'Export result' })).toBeEnabled()
  const changed = '66666666-6666-4666-8666-666666666666'
  const nextProject = { ...project, ...(scope === 'project' ? { project_id: changed } : scope === 'organization' ? { org_id: changed } : {}) }
  const nextJob = { ...job, ...(scope === 'job' ? { job_id: changed } : scope === 'input version' ? { input_version_id: changed } : {}) }
  rerender(<ArloProjectJob project={nextProject} job={nextJob} />)
  const download = screen.getByRole('button', { name: 'Export result' })
  expect(download).toBeDisabled()
  expect(screen.getByText(/Export unavailable: the opened result belongs to a different project or job/)).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Accept proposal' })).toBeNull()
  fireEvent.click(download)
  expect(captured.createObjectURL).not.toHaveBeenCalled()
})

it('ignores a pending canonical open after switching projects', async () => {
  let finish
  openProject.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender } = render(<ArloProjectJob project={project} job={job} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  rerender(<ArloProjectJob project={{ ...project, project_id: '66666666-6666-4666-8666-666666666666' }} job={job} />)
  await act(async () => finish(workspace))
  expect(screen.queryByRole('region', { name: 'Saved feeder proposal' })).toBeNull()
  expect(getArloReviews).not.toHaveBeenCalled()
})

it.each(['project_id', 'organization_id', 'input_version_id'])('disables export when the canonical job envelope has another %s', async key => {
  const captured = captureDownload()
  openProject.mockResolvedValue({ ...workspace, jobs: [{ ...job, result: { ...job.result,
    solver_input: { ...job.result.solver_input, [key]: '66666666-6666-4666-8666-666666666666' } } }] })
  render(<WorkspaceSummary workspace={workspace} />)
  fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
  await screen.findByRole('region', { name: 'Saved feeder proposal' })
  expect(screen.getByRole('button', { name: 'Export result' })).toBeDisabled()
  expect(screen.getByText(/Export unavailable: this stored result does not match the current project job/)).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
  expect(captured.createObjectURL).not.toHaveBeenCalled()
  expect(getArloReviews).not.toHaveBeenCalled()
})

it('opens the canonical saved job and records a proposal decision in its existing review API', async () => {
  render(<WorkspaceSummary workspace={workspace} />)
  fireEvent.click(screen.getByRole('button',{name:'Open feeder proposal'}))
  await screen.findByRole('region',{name:'Saved feeder proposal'})
  expect(openProject).toHaveBeenCalledWith(projectId,org)
  await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
  fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
  await screen.findByText('Proposal accepted for the next CAD step.')
  expect(saveArloReview.mock.calls[0][0]).toEqual({org_id:org,project_id:projectId,job_id:jobId,input_version_id:version})
  fireEvent.click(screen.getByRole('button',{name:'Close proposal'}))
  expect(screen.queryByRole('region',{name:'Saved feeder proposal'})).toBeNull()
})

it('refuses a response belonging to another project without opening review controls', async () => {
  openProject.mockResolvedValue({...workspace,project:{...project,project_id:'other'}})
  render(<WorkspaceSummary workspace={workspace} />)
  fireEvent.click(screen.getByRole('button',{name:'Open feeder proposal'}))
  await screen.findByRole('alert')
  expect(getArloReviews).not.toHaveBeenCalled()
  expect(screen.queryByRole('button',{name:'Accept proposal'})).toBeNull()
})

it('does not offer a review action for cancelled or non-ARLO jobs', () => {
  render(<WorkspaceSummary workspace={{...workspace,jobs:[{...job,status:'cancelled'},{...job,job_id:'legacy',tool_name:'other'}]}} />)
  expect(screen.queryByRole('button',{name:'Open feeder proposal'})).toBeNull()
})
