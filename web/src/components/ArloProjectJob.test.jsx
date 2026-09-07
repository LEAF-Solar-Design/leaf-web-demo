import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { openProject, getArloReviews, saveArloReview } from '../api.js'
import WorkspaceSummary from './WorkspaceSummary.jsx'

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
afterEach(() => { cleanup(); vi.resetAllMocks() })

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
