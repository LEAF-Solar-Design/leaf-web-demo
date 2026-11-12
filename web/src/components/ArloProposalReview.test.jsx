import React from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getArloReviews, saveArloReview } from '../api.js'
import ArloProposalReview from './ArloProposalReview.jsx'
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
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const panel = result => <ResultPanel running={false} result={result} tool={{name:'arlo-design'}} />
function captureDownload() {
  const createObjectURL = vi.fn(() => 'blob:arlo-result'), revokeObjectURL = vi.fn()
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createObjectURL
    static revokeObjectURL = revokeObjectURL
  })
  let anchor
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function () { anchor = this })
  return { createObjectURL, revokeObjectURL, anchor: () => anchor }
}
function readJson(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(JSON.parse(reader.result))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(blob)
  })
}
function detailedEnvelope() {
  const stored = envelope()
  Object.assign(stored.result.solver_input, {
    scenario: { source_ids: ['source-panel', 'source-load'], units: 'm' },
    catalog: [{ id: 'catalog-conduit', kind: 'conduit' }],
    requirements: { max_support_spacing: 2.4, access_clearance: 0.6 },
  })
  Object.assign(stored.result, {
    request_sha256: 'b'.repeat(64), input_sha256: 'c'.repeat(64), source_sha256: 'd'.repeat(64),
    solver_revision: 'solver-revision-test', runtime: 'test-runtime',
    limitations: ['Native host verification pending.'],
  })
  Object.assign(stored.result.solver_result, {
    request_hash: 'e'.repeat(64), production_valid: false,
    limitations: ['Solver geometry only.'],
    trace: Array.from({ length: 125 }, (_, index) => ({ event: 'candidate_checked', index, source_id: `source-${index}` })),
  })
  stored.result.solver_result.proposals = stored.result.solver_result.proposals.map((item, index) => ({
    ...item,
    source: { ...stored.result.solver_input, request_hash: stored.result.solver_result.request_hash },
    routes: [{ id: `route-${index}`, points: [{ x: 0.123456789, y: 1, z: 3 }, { x: 2, y: 1, z: 3 }],
      segments: [{ start: 0, end: 1, catalog_id: 'catalog-conduit' }] }],
    selected_parts: [{ catalog_id: 'catalog-conduit', source_id: 'source-panel', quantity: 2 }],
    placements: [{ id: `support-${index}`, kind: 'support', position: { x: 1, y: 1, z: 3 },
      attachment_position: { x: 1, y: 1, z: 4 }, host_id: 'host-beam', catalog_id: 'catalog-support' }],
    cost_breakdown: [{ kind: 'material', cost: 8 }, { kind: 'labor', cost: 4.5 }],
    constraints: [{ name: 'support_spacing', satisfied: true, maximum: 2.4 }],
    violations: index ? [{ code: 'access_pending', message: 'Access check pending.', source_id: 'source-load' }] : [],
    limitations: [`Proposal ${index + 1} fitting offsets unverified.`],
  }))
  return stored
}

describe('ARLO review in the existing workspace result panel', () => {
  it('inspects every stored proposal field and downloads exactly the full JSON preview', async () => {
    const captured = captureDownload(), stored = detailedEnvelope()
    render(panel(stored))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeEnabled())
    expect(screen.getByText('Production valid:')).toHaveTextContent('Production valid: false')
    const limitations = screen.getByRole('region', { name: 'Limitations' })
    for (const phrase of ['Native boxes and supports', 'host attachment', 'family compatibility', 'engineering compliance', 'fitting offsets', 'volumetric/access envelopes', 'native CAD round trip']) {
      expect(limitations).toHaveTextContent(phrase)
    }
    expect(limitations).toHaveTextContent('Native host verification pending.')
    expect(limitations).toHaveTextContent('Solver geometry only.')
    expect(limitations).toHaveTextContent('Proposal 1 fitting offsets unverified.')
    expect(screen.getByText('Trace event count: 125')).toBeInTheDocument()
    expect(JSON.parse(screen.getByLabelText('Trace events JSON').textContent)).toEqual(stored.result.solver_result.trace)
    fireEvent.click(screen.getByText('Inspect full stored result and export JSON'))
    const preview = JSON.parse(screen.getByLabelText('Export JSON preview').textContent)
    expect(preview.envelope).toEqual(stored.result)
    expect(preview.job_context).toEqual(context)
    const download = screen.getByRole('button', { name: 'Export result' })
    expect(download).toBeEnabled()
    download.focus()
    expect(download).toHaveFocus()
    for (const item of stored.result.solver_result.proposals) {
      fireEvent.change(screen.getByLabelText('Feeder proposal'), { target: { value: item.proposal_id } })
      const proposalPreview = JSON.parse(screen.getByLabelText('Proposal JSON').textContent)
      expect(proposalPreview).toEqual(item)
      fireEvent.click(download)
      const blob = captured.createObjectURL.mock.calls.at(-1)[0]
      expect(blob.type).toBe('application/json')
      const exported = await readJson(blob)
      expect(exported).toEqual(preview)
      expect(exported.envelope.solver_result.proposals.find(p => p.proposal_id === item.proposal_id)).toEqual(proposalPreview)
      expect(exported.product_disclaimer).toContain('remain unverified')
      expect(captured.anchor().download).toBe(`arlo-result-${context.job_id}.json`)
      expect(captured.anchor().getAttribute('href')).toBe('blob:arlo-result')
      expect(captured.anchor().isConnected).toBe(false)
    }
    await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledTimes(2))
    expect(captured.revokeObjectURL).toHaveBeenCalledWith('blob:arlo-result')
    expect(saveArloReview).not.toHaveBeenCalled()
  })

  it('disables export with a visible reason when the stored result is absent', () => {
    const captured = captureDownload()
    render(<ArloProposalReview context={context} />)
    const download = screen.getByRole('button', { name: 'Export result' })
    expect(download).toBeDisabled()
    expect(screen.getByText('No stored result is available to export.')).toBeVisible()
    fireEvent.click(download)
    expect(captured.createObjectURL).not.toHaveBeenCalled()
    expect(getArloReviews).not.toHaveBeenCalled()
  })

  it('refuses export when a stored result is carried into another project', async () => {
    const captured = captureDownload(), stored = detailedEnvelope()
    const { rerender } = render(<ArloProposalReview envelope={stored.result} context={context} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Export result' })).toBeEnabled())
    rerender(<ArloProposalReview envelope={stored.result} context={{ ...context, project_id: '66666666-6666-4666-8666-666666666666' }} />)
    expect(screen.getByRole('button', { name: 'Export result' })).toBeDisabled()
    expect(screen.getByText(/Export unavailable: this stored result does not match the current project job/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
    expect(captured.createObjectURL).not.toHaveBeenCalled()
    expect(saveArloReview).not.toHaveBeenCalled()
    expect(getArloReviews).toHaveBeenCalledTimes(1)
  })

  it('does not invent missing proposal data or production validity', () => {
    const stored = envelope()
    stored.result.solver_result.proposals = [{ proposal_id: 'sparse-proposal' }]
    render(panel(stored))
    expect(screen.getByText('Production valid:')).toHaveTextContent('Not supplied')
    expect(JSON.parse(screen.getByLabelText('Proposal JSON').textContent)).toEqual({ proposal_id: 'sparse-proposal' })
    expect(JSON.parse(screen.getByLabelText('Export JSON preview').textContent).envelope).toEqual(stored.result)
  })

  it('cleans up the Blob URL and reports a failed download', async () => {
    const captured = captureDownload()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error('Download blocked') })
    render(panel(envelope()))
    fireEvent.click(screen.getByRole('button', { name: 'Export result' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Result export failed. Download blocked')
    expect(document.querySelector('a[download]')).toBeNull()
    await waitFor(() => expect(captured.revokeObjectURL).toHaveBeenCalledWith('blob:arlo-result'))
  })

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
    expect(container.textContent).not.toContain('2026-09-06T19:00:00Z')
    expect(container.textContent).not.toContain('review-reject')
    expect(saveArloReview).not.toHaveBeenCalled()
  })

  it('keeps failed writes unconfirmed and retries with the same idempotency key', async () => {
    saveArloReview.mockRejectedValueOnce(new Error('Connection lost'))
    render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Reject proposal'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'Reject proposal'}))
    expect(await screen.findByRole('alert')).toHaveTextContent('Decision not confirmed saved. Connection lost')
    expect(screen.queryByText('Proposal rejected.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button',{name:'Reject proposal'}))
    await screen.findByText('Proposal rejected.')
    expect(saveArloReview.mock.calls[0][2]).toBe(saveArloReview.mock.calls[1][2])
  })

  it('keeps a lost-response key when switching proposals and retrying the decision', async () => {
    saveArloReview.mockRejectedValueOnce(new Error('Connection lost'))
    render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Review note'),{target:{value:'Ready for review.'}})
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByRole('alert')
    const firstAttempt = saveArloReview.mock.calls[0]
    fireEvent.change(screen.getByLabelText('Feeder proposal'),{target:{value:'proposal-b'}})
    fireEvent.change(screen.getByLabelText('Feeder proposal'),{target:{value:'proposal-a'}})
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByText('Proposal accepted for the next CAD step.')
    expect(saveArloReview).toHaveBeenCalledTimes(2)
    expect(firstAttempt[2]).toEqual(expect.any(String))
    expect(saveArloReview.mock.calls[1][2]).toBe(firstAttempt[2])
    expect(saveArloReview.mock.calls[1][1]).toEqual(firstAttempt[1])
  })

  it('uses a new key for a new decision after confirmed success', async () => {
    render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByText('Proposal accepted for the next CAD step.')
    const firstKey = saveArloReview.mock.calls[0][2]
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await waitFor(() => expect(saveArloReview).toHaveBeenCalledTimes(2))
    await screen.findByText('Proposal accepted for the next CAD step.')
    expect(firstKey).toEqual(expect.any(String))
    expect(saveArloReview.mock.calls[1][2]).toEqual(expect.any(String))
    expect(saveArloReview.mock.calls[1][2]).not.toBe(firstKey)
    expect(saveArloReview.mock.calls[1][1]).toEqual(saveArloReview.mock.calls[0][1])
  })

  it('never replays a superseded acceptance key after a later rejection succeeds', async () => {
    saveArloReview.mockRejectedValueOnce(new Error('Connection lost'))
    const {container}=render(panel(envelope()))
    await waitFor(() => expect(screen.getByRole('button',{name:'Accept proposal'})).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Review note'),{target:{value:'First look.'}})
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByRole('alert')
    fireEvent.change(screen.getByLabelText('Review note'),{target:{value:'Rejecting for now.'}})
    fireEvent.click(screen.getByRole('button',{name:'Reject proposal'}))
    await screen.findByText('Proposal rejected.')
    fireEvent.change(screen.getByLabelText('Review note'),{target:{value:'Accepted after rework.'}})
    fireEvent.click(screen.getByRole('button',{name:'Accept proposal'}))
    await screen.findByText('Proposal accepted for the next CAD step.')
    expect(saveArloReview).toHaveBeenCalledTimes(3)
    const keys = saveArloReview.mock.calls.map(call => call[2])
    expect(new Set(keys).size).toBe(3)
    expect(saveArloReview.mock.calls[2][1]).toEqual({proposal_id:'proposal-a',result_sha256:'a'.repeat(64),decision:'accept',note:'Accepted after rework.'})
    const history = container.querySelectorAll('.arlo-review ol > li')
    expect(history).toHaveLength(2)
    expect(history[0]).toHaveTextContent('Rejected: Rejecting for now.')
    expect(history[1]).toHaveTextContent('Accepted: Accepted after rework.')
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
    expect(screen.getByRole('button',{name:'Export result'})).toBeDisabled()
    expect(screen.getByText('Open this result from its project job to save a decision.')).toBeInTheDocument()
    expect(getArloReviews).not.toHaveBeenCalled()
  })
})
