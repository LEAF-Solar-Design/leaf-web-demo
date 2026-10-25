import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import ClaudeAccountPanel from './ClaudeAccountPanel.jsx'
import CostTransparencyPanel from './CostTransparencyPanel.jsx'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const title = 'What Leaf costs to operate'
const accountProps = {
  open: true, loading: false, busy: false, grant: { linked: false },
  onToggle: () => {}, onUnlink: () => {},
}

function costResponses() {
  const fetchMock = vi.fn(async (url) => new Response(JSON.stringify(
    String(url).includes('/history?') ? { months: [], resources: [] } : { publication_id: null },
  ), { status: 200 }))
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

async function openCosts() {
  render(<CostTransparencyPanel />)
  fireEvent.click(screen.getByRole('button', { name: title }))
  await screen.findByText('The publication for this month has not been made yet.')
}

describe('account and cost issue sweep', () => {
  it('S1-62 retains a rejected token and the existing failure feedback', async () => {
    const onLink = vi.fn().mockResolvedValue(null)
    render(<ClaudeAccountPanel {...accountProps} onLink={onLink} error="This token was rejected. Try another token." />)
    fireEvent.click(screen.getByRole('radio', { name: 'Anthropic API key' }))
    const input = screen.getByLabelText('Anthropic API key')
    fireEvent.change(input, { target: { value: 'rejected-token' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(onLink).toHaveBeenCalledTimes(1)
    expect(input).toHaveValue('rejected-token')
    expect(screen.getByRole('alert')).toHaveTextContent('This token was rejected. Try another token.')
  })

  it('S1-62 clears the credential after a successful grant', async () => {
    render(<ClaudeAccountPanel {...accountProps} onLink={vi.fn().mockResolvedValue({ linked: true })} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Anthropic API key' }))
    const input = screen.getByLabelText('Anthropic API key')
    fireEvent.change(input, { target: { value: 'accepted-token' } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Link API key' })) })
    expect(input).toHaveValue('')
  })

  it('S1-63 shows a body status while checking the account grant', () => {
    render(<ClaudeAccountPanel {...accountProps} loading grant={null} />)
    const dialog = screen.getByRole('dialog', { name: 'Claude accounts' })
    expect(within(dialog).getByRole('status')).toHaveTextContent('Checking your Claude accounts...')
    expect(within(dialog).queryByLabelText('Claude token')).not.toBeInTheDocument()
  })

  it('S1-59 explains sign-in and hides controls that cannot read guest costs', () => {
    const fetchMock = costResponses()
    render(<CostTransparencyPanel readsPaused />)
    fireEvent.click(screen.getByRole('button', { name: title }))
    expect(screen.getByRole('status')).toHaveTextContent('Sign in to view your costs and cost history.')
    expect(screen.queryByRole('button', { name: /Retry costs|Retry history|Monthly|History/ })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/month/i)).not.toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('S1-67 retains partial monthly text until a valid month is committed on blur', async () => {
    const fetchMock = costResponses()
    await openCosts()
    const input = screen.getByLabelText('Month')
    // Simulate a browser where the month input falls back to text.
    input.setAttribute('type', 'text')
    fireEvent.change(input, { target: { value: '2025-' } })
    expect(input).toHaveValue('2025-')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fireEvent.change(input, { target: { value: '2025-01' } })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    fireEvent.blur(input)
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining('period=2025-01'), expect.any(Object)))
  })

  it('S1-67 allows history month drafts and validates them on Enter or blur', async () => {
    const fetchMock = costResponses()
    await openCosts()
    fireEvent.click(screen.getByRole('button', { name: 'History' }))
    const input = await screen.findByLabelText('From month')
    const initial = input.value
    const previous = new Date(`${initial}-01T00:00:00Z`)
    previous.setUTCMonth(previous.getUTCMonth() - 1)
    const valid = previous.toISOString().slice(0, 7)
    input.setAttribute('type', 'text')
    fireEvent.change(input, { target: { value: valid.slice(0, 5) } })
    expect(input).toHaveValue(valid.slice(0, 5))
    fireEvent.change(input, { target: { value: valid } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(fetchMock).toHaveBeenLastCalledWith(expect.stringContaining(`from=${valid}`), expect.any(Object)))
    const to = await screen.findByLabelText('To month')
    const initialTo = to.value
    to.setAttribute('type', 'text')
    fireEvent.change(to, { target: { value: '2099-' } })
    expect(to).toHaveValue('2099-')
    fireEvent.blur(to)
    expect(to).toHaveValue(initialTo)
  })
})
