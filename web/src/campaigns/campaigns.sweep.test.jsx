import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CampaignPanel from './CampaignPanel.jsx'
import useCampaigns from './useCampaigns.js'
import { uploadProjectInput } from './api.js'
import { subscribeUnauthorized } from '../api.js'
import { getProjectLifecycle } from '../projects/api.js'

vi.mock('./useCampaigns.js', async importOriginal => ({ ...await importOriginal(), default: vi.fn() }))
vi.mock('./api.js', async importOriginal => ({ ...await importOriginal(), uploadProjectInput: vi.fn() }))

let campaign
beforeEach(() => {
  const row = { campaign_id: '33333333-3333-3333-3333-333333333333', title: 'Solar layout', prompt: 'Prepare the roof layout.', status: 'accepted' }
  campaign = {
    status: 'ready', campaigns: [row], selectedId: row.campaign_id, selected: row,
    questions: [], answers: {}, pending: {},
    ask: vi.fn().mockResolvedValue({ question: { question_id: 'recorded' } }),
    createRelease: vi.fn().mockResolvedValue({ ok: true }),
    submit: vi.fn().mockResolvedValue({ campaign: row }), refetch: vi.fn(), select: vi.fn(),
  }
  uploadProjectInput.mockReset()
  useCampaigns.mockImplementation(() => campaign)
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  localStorage.clear()
})

function show() {
  const view = render(<CampaignPanel projectId="11111111-1111-1111-1111-111111111111" signedIn />)
  view.container.querySelectorAll('details:not([open]) > summary').forEach(summary => fireEvent.click(summary))
  return view
}

describe('campaign sweep regressions', () => {
  it('restores typed drafts after the sign-in recovery prompt', () => {
    const view = show()
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Roof draft' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Keep this goal.' } })
    fireEvent.change(screen.getByLabelText('Follow-up question'), { target: { value: 'Which roof?' } })
    fireEvent.change(screen.getByLabelText('Release workflow'), { target: { value: 'Keep this release.' } })
    view.rerender(<CampaignPanel projectId="11111111-1111-1111-1111-111111111111" signedIn={false} />)
    expect(screen.getByText('Sign in to submit a campaign.')).toBeTruthy()
    expect(screen.queryByLabelText('Prompt')).toBeNull()
    view.rerender(<CampaignPanel projectId="11111111-1111-1111-1111-111111111111" signedIn />)
    expect(screen.getByLabelText('Title').value).toBe('Roof draft')
    expect(screen.getByLabelText('Prompt').value).toBe('Keep this goal.')
    expect(screen.getByLabelText('Follow-up question').value).toBe('Which roof?')
    expect(screen.getByLabelText('Release workflow').value).toBe('Keep this release.')
  })

  it('does not restore another project’s typed drafts', () => {
    const view = show()
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Private project goal.' } })
    view.rerender(<CampaignPanel projectId="22222222-2222-2222-2222-222222222222" signedIn />)
    expect(screen.getByLabelText('Prompt').value).toBe('')
  })

  for (const [name, request] of [
    ['campaigns', async () => (await import('./api.js')).listCampaigns('11111111-1111-1111-1111-111111111111')],
    ['project lifecycle', () => getProjectLifecycle('11111111-1111-1111-1111-111111111111')],
  ]) {
    it(`opens shared sign-in recovery for an expired ${name} token`, async () => {
      localStorage.setItem('leaf.jwt', 'expired-token')
      const notified = vi.fn()
      const unsubscribe = subscribeUnauthorized(notified)
      const response = { ok: false, status: 401, json: async () => ({}) }
      response.clone = () => response
      const fetcher = vi.fn().mockResolvedValue(response)
      vi.stubGlobal('fetch', fetcher)
      try {
        await expect(request()).rejects.toMatchObject({ status: 401 })
        expect(fetcher.mock.calls[0][1].headers.Authorization).toBe('Bearer expired-token')
        expect(localStorage.getItem('leaf.jwt')).toBeNull()
        expect(notified).toHaveBeenCalledTimes(1)
      } finally { unsubscribe() }
    })

    it(`keeps a newer sign-in when an older ${name} request returns unauthorized`, async () => {
      localStorage.setItem('leaf.jwt', 'old-token')
      const notified = vi.fn()
      const unsubscribe = subscribeUnauthorized(notified)
      let resolve
      const fetcher = vi.fn(() => new Promise(done => { resolve = done }))
      vi.stubGlobal('fetch', fetcher)
      try {
        const pending = request()
        await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1))
        localStorage.setItem('leaf.jwt', 'new-token')
        const response = { ok: false, status: 401, json: async () => ({}) }
        response.clone = () => response
        resolve(response)
        await expect(pending).rejects.toMatchObject({ status: 401 })
        expect(fetcher.mock.calls[0][1].headers.Authorization).toBe('Bearer old-token')
        expect(localStorage.getItem('leaf.jwt')).toBe('new-token')
        expect(notified).not.toHaveBeenCalled()
      } finally { unsubscribe() }
    })
  }

  it('lets a long campaign goal be shortened before requesting its release', async () => {
    campaign.selected = { ...campaign.selected, prompt: 'x'.repeat(2001) }
    show()
    const workflow = screen.getByLabelText('Release workflow')
    expect(workflow.value).toBe(campaign.selected.prompt)
    expect(workflow.maxLength).toBe(2000)
    fireEvent.click(screen.getByRole('button', { name: 'Finish this project' }))
    await screen.findByText('Workflow must contain 1 to 2000 characters.')
    expect(campaign.createRelease).not.toHaveBeenCalled()
    fireEvent.change(workflow, { target: { value: 'Deliver the roof layout.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Finish this project' }))
    await screen.findByText('Release requested.')
    expect(campaign.createRelease).toHaveBeenCalledWith(expect.objectContaining({ workflow: 'Deliver the roof layout.' }))
  })

  it('rejects an empty replacement workflow without sending a request', async () => {
    show()
    fireEvent.change(screen.getByLabelText('Release workflow'), { target: { value: ' ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Finish this project' }))
    await screen.findByText('Workflow must contain 1 to 2000 characters.')
    expect(campaign.createRelease).not.toHaveBeenCalled()
  })

  it('clears a recorded question so a second click cannot duplicate it', async () => {
    show()
    const field = screen.getByLabelText('Follow-up question')
    fireEvent.change(field, { target: { value: 'Which roof?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await screen.findByText('Question recorded.')
    expect(field.value).toBe('')
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await screen.findByText('Question must contain 1 to 4096 characters.')
    expect(campaign.ask).toHaveBeenCalledTimes(1)
  })

  it('preserves edits made while a question is being recorded', async () => {
    let resolve
    campaign.ask.mockImplementation(() => new Promise(done => { resolve = done }))
    show()
    const field = screen.getByLabelText('Follow-up question')
    fireEvent.change(field, { target: { value: 'Which roof?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    fireEvent.change(field, { target: { value: 'Which panel?' } })
    await act(async () => resolve({ question: { question_id: 'recorded' } }))
    expect(field.value).toBe('Which panel?')
  })

  it('retains a question after an uncertain failure and hides transport text', async () => {
    campaign.ask.mockRejectedValue(new Error('POST /api/campaigns/questions -> 502'))
    show()
    const field = screen.getByLabelText('Follow-up question')
    fireEvent.change(field, { target: { value: 'Which roof?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask' }))
    await screen.findByText('The service had a temporary problem. Try again.')
    expect(field.value).toBe('Which roof?')
    expect(screen.queryByText('Question recorded.')).toBeNull()
  })

  it('explains that an uploaded file stays in the project instead of offering removal', async () => {
    const path = 'inputs/roof/roof.txt'
    uploadProjectInput.mockResolvedValue({ path, name: 'roof.txt' })
    show()
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'finish' } })
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Roof layout' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Deliver the roof layout.' } })
    fireEvent.change(screen.getByLabelText('Input file (optional)'), {
      target: { files: [new File(['roof'], 'roof.txt', { type: 'text/plain' })] },
    })
    expect(screen.getByRole('button', { name: 'Remove selected file' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Add to project' }))
    await screen.findByText('roof.txt added to this project. Ready for this release.')
    expect(screen.queryByRole('button', { name: 'Remove selected file' })).toBeNull()
    expect(screen.getByText('This file was added to the project and stays there.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Request release' }).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Request release' }))
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith(expect.objectContaining({
      finish: expect.objectContaining({ artifact_refs: [path] }),
    })))
  })

  it('removes an unwanted input from both state and the file picker', async () => {
    show()
    fireEvent.change(screen.getByLabelText('Campaign goal'), { target: { value: 'finish' } })
    fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'Roof layout' } })
    fireEvent.change(screen.getByLabelText('Prompt'), { target: { value: 'Deliver the roof layout.' } })
    const input = screen.getByLabelText('Input file (optional)')
    // Model the browser's selected value so clearing only React state fails this case.
    Object.defineProperty(input, 'value', { configurable: true, writable: true, value: 'C:\\fakepath\\wrong.txt' })
    fireEvent.change(input, { target: { files: [new File(['wrong'], 'wrong.txt', { type: 'text/plain' })] } })
    expect(screen.getByRole('button', { name: 'Request release' }).disabled).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Remove selected file' }))
    expect(input.value).toBe('')
    expect(screen.queryByRole('button', { name: 'Remove selected file' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Request release' }))
    await waitFor(() => expect(campaign.submit).toHaveBeenCalledWith(expect.objectContaining({
      finish: expect.objectContaining({ artifact_refs: [] }),
    })))
    expect(uploadProjectInput).not.toHaveBeenCalled()
  })
})
