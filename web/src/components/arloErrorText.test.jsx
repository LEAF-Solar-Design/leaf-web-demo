import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getArloReviews, openProject, proposePlatformChange, saveArloReview } from '../api.js'
import ArloProposalReview from './ArloProposalReview.jsx'
import ArloProjectJob from './ArloProjectJob.jsx'
import CustomizePanel from './CustomizePanel.jsx'

vi.mock('../api.js', () => ({
  getArloReviews: vi.fn(), saveArloReview: vi.fn(), openProject: vi.fn(),
  getPlatformChange: vi.fn(), landPlatformChange: vi.fn(), mergePlatformChange: vi.fn(), proposePlatformChange: vi.fn(),
}))

const context = {
  org_id: '11111111-1111-4111-8111-111111111111',
  project_id: '22222222-2222-4222-8222-222222222222',
  job_id: '33333333-3333-4333-8333-333333333333',
  input_version_id: '44444444-4444-4444-8444-444444444444',
}
const envelope = {
  result_sha256: 'a'.repeat(64),
  solver_input: { organization_id: context.org_id, project_id: context.project_id, input_version_id: context.input_version_id },
  solver_result: { status: 'complete', proposals: [{ proposal_id: 'proposal-a', production_valid: false, violations: [] }], trace: [] },
}

beforeEach(() => {
  getArloReviews.mockResolvedValue({ job_id: context.job_id, result_sha256: envelope.result_sha256, reviews: [] })
})
afterEach(() => { cleanup(); vi.resetAllMocks() })

describe('readable errors on proposal and change screens', () => {
  it('replaces a reviews GET transport error with a service message', async () => {
    getArloReviews.mockRejectedValueOnce(new Error(`GET /api/projects/${context.project_id}/arlo-jobs/${context.job_id}/reviews -> 500`))
    render(<ArloProposalReview envelope={envelope} context={context} />)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The service had a temporary problem. Try again.')
    expect(alert).not.toHaveTextContent(/GET|\/api\/|500|33333333/)
  })

  it('keeps a failed decision unconfirmed while replacing the fetch error', async () => {
    saveArloReview.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    render(<ArloProposalReview envelope={envelope} context={context} />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Accept proposal' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Accept proposal' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Decision not confirmed saved. Couldn't reach the service. Check the connection and retry.")
    expect(alert).not.toHaveTextContent('Failed to fetch')
    expect(screen.queryByText('Proposal accepted for the next CAD step.')).not.toBeInTheDocument()
  })

  it('replaces project opening transport errors with a service message', async () => {
    openProject.mockRejectedValueOnce(new Error(`GET /api/projects/${context.project_id}/open -> 500`))
    render(<ArloProjectJob project={context} job={{ job_id: context.job_id }} />)
    fireEvent.click(screen.getByRole('button', { name: 'Open feeder proposal' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The service had a temporary problem. Try again.')
    expect(alert).not.toHaveTextContent(/GET|\/api\/|500/)
    expect(screen.queryByRole('region', { name: 'Saved feeder proposal' })).not.toBeInTheDocument()
  })

  it('humanizes the Customize panel fallback when proposing fails', async () => {
    proposePlatformChange.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    render(<CustomizePanel tenant="error-text-test" />)
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Update wording' } })
    fireEvent.change(screen.getByLabelText('Path 1'), { target: { value: 'web/src/example.jsx' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Couldn't reach the service. Check the connection and retry.")
    expect(alert).not.toHaveTextContent('Failed to fetch')
  })
})
