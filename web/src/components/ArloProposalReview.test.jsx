import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getArloReviews, saveArloReview } from '../api.js'
import ResultPanel from './ResultPanel.jsx'

vi.mock('../api.js', () => ({ getArloReviews:vi.fn(), saveArloReview:vi.fn() }))
const context = { org_id:'11111111-1111-4111-8111-111111111111', project_id:'22222222-2222-4222-8222-222222222222', job_id:'33333333-3333-4333-8333-333333333333', input_version_id:'44444444-4444-4444-8444-444444444444' }
const proposal = { proposal_id:'proposal-a', production_valid:false, violations:[], estimated_installed_cost:12.5,
  quantities:[{kind:'conduit',quantity:2,unit:'m',installed_cost:12.5}], routes:[{points:[{x:0,y:0,z:3},{x:2,y:0,z:3}]}], placements:[], source:{} }
function envelope(jobContext=context) {
  return {ok:true,tool:'arlo-design',timing_ms:null,cost:null,job_context:jobContext,result:{solver:'arlo-design', result_sha256:'a'.repeat(64),
    solver_input:{organization_id:jobContext.org_id,project_id:jobContext.project_id,input_version_id:jobContext.input_version_id},
    solver_result:{status:'complete',proposals:[proposal,{...proposal,proposal_id:'proposal-b',estimated_installed_cost:15}],trace:[{event:'rejected_design',message:'Supplied support did not reach.'}]}}}
}
const row = (decision='accept', job=context.job_id, note='') => ({operation_id:'review-'+decision,payload:{jobId:job,resultHash:'a'.repeat(64),proposalId:'proposal-a',decision,note},created_at:'2026-09-06T19:00:00Z'})
beforeEach(() => {
  getArloReviews.mockResolvedValue({job_id:context.job_id,result_sha256:'a'.repeat(64),reviews:[]})
  saveArloReview.mockImplementation(async (_ctx,body) => ({review:row(body.decision,context.job_id,body.note)}))
})
afterEach(() => { cleanup(); vi.clearAllMocks() })
const panel = result => <ResultPanel running={false} result={result} tool={{name:'arlo-design'}} />

describe('ARLO review in the existing workspace result panel', () => {
  it('renders actual proposal geometry and takeoff, then saves an exact choice', async () => {
    render(panel(envelope()))
    expect(screen.getByRole('img',{name:/Projected solver/})).toBeInTheDocument()
    expect(screen.getByText('12.5 catalog cost units')).toBeInTheDocument()
    expect(screen.getByText('Compute cost unavailable')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Review note'),{target:{value:'Looks ready for CAD review.'}})
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByText('Proposal accepted for the next CAD step.')
    expect(saveArloReview.mock.calls[0][1]).toEqual({proposal_id:'proposal-a',result_sha256:'a'.repeat(64),decision:'accept',note:'Looks ready for CAD review.'})
    expect(screen.getByText(/does not apply CAD changes/)).toBeInTheDocument()
  })

  it('reopens saved decisions from the canonical service and escapes notes', async () => {
    getArloReviews.mockResolvedValue({job_id:context.job_id,result_sha256:'a'.repeat(64),reviews:[row('reject',context.job_id,'<script>bad()</script>')]})
    const {container}=render(panel(envelope()))
    await screen.findByText(/<script>bad/)
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText('Rejected')).toBeInTheDocument()
    expect(saveArloReview).not.toHaveBeenCalled()
  })

  it('keeps failed writes unconfirmed and retries with the same idempotency key', async () => {
    saveArloReview.mockRejectedValueOnce(new Error('Connection lost'))
    render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Reject proposal'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'Reject proposal'}))
    await screen.findByRole('alert')
    expect(screen.queryByText('Proposal rejected.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'Reject proposal'}))
    await screen.findByText('Proposal rejected.')
    expect(saveArloReview.mock.calls[0][2]).toBe(saveArloReview.mock.calls[1][2])
  })

  it('does not let a stale save response populate a different job', async () => {
    let finish
    saveArloReview.mockImplementationOnce(() => new Promise(resolve => { finish=resolve }))
    const {rerender}=render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    const next={...context,job_id:'55555555-5555-4555-8555-555555555555'}
    getArloReviews.mockResolvedValue({job_id:next.job_id,result_sha256:'a'.repeat(64),reviews:[]})
    rerender(panel(envelope(next)))
    await act(async () => finish({review:row()}))
    expect(screen.queryByText('Proposal accepted for the next CAD step.')).not.toBeInTheDocument()
    expect(screen.queryByText('Accepted')).not.toBeInTheDocument()
  })

  it('shows unbound results without executable review controls', () => {
    const result=envelope();delete result.job_context
    render(panel(result))
    expect(screen.getByRole('button',{name:'Accept proposal'})).toBeDisabled()
    expect(getArloReviews).not.toHaveBeenCalled()
  })
})
