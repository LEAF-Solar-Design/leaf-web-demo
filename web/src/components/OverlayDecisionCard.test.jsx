/**
 * T1 overlay decision card. Each test names the failure it prevents; the two
 * the adversarial review called out by name are the deny control and the
 * untrusted-text rendering.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import OverlayDecisionCard from './OverlayDecisionCard.jsx'

afterEach(cleanup)

const PROPOSAL = {
  proposal_id: 'p-1',
  requested_by: 'user@example.com',
  request_text: 'make the background light mode',
  tokens: { 'color.canvas.bg': '#ffffff', 'copy.home.title': 'Light mode' },
}

const setup = (over = {}) => {
  const onDecide = over.onDecide || vi.fn().mockResolvedValue(undefined)
  render(
    <OverlayDecisionCard
      proposal={over.proposal === undefined ? PROPOSAL : over.proposal}
      documentVersion={over.documentVersion ?? 7}
      labels={{ 'color.canvas.bg': 'Workspace background' }}
      onDecide={onDecide}
      busy={over.busy}
    />,
  )
  return { onDecide }
}

describe('both controls exist', () => {
  it('renders a DENY control, not just approve', () => {
    // THE review finding: "one button" left a proposal the operator would not
    // approve with no way to close it, stranding the requester's preview.
    setup()
    expect(screen.getByRole('button', { name: /deny/i })).toBeTruthy()
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
  })

  it('says approval is scoped to this tenant', () => {
    setup()
    expect(screen.getByRole('button', { name: /applies to this tenant/i })).toBeTruthy()
  })
})

describe('the decision it sends', () => {
  it('carries the CAS witness the card was rendered against', async () => {
    const { onDecide } = setup({ documentVersion: 42 })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    await waitFor(() => expect(onDecide).toHaveBeenCalled())
    expect(onDecide.mock.calls[0][1].documentVersion).toBe(42)
    expect(onDecide.mock.calls[0][1].approve).toBe(true)
  })

  it('sends approve:false for deny', async () => {
    const { onDecide } = setup()
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    await waitFor(() => expect(onDecide).toHaveBeenCalled())
    expect(onDecide.mock.calls[0][1].approve).toBe(false)
  })

  it('generates a decision key per TAP', async () => {
    const { onDecide } = setup()
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    await waitFor(() => expect(onDecide).toHaveBeenCalled())
    const key = onDecide.mock.calls[0][1].decisionKey
    expect(typeof key).toBe('string')
    expect(key.length).toBeGreaterThanOrEqual(8)
  })

  it('a double-click cannot fire two decisions', async () => {
    // Server-side idempotency is the real guarantee; this is the cheap half.
    const onDecide = vi.fn(() => new Promise(() => {})) // never settles
    setup({ onDecide })
    const approve = screen.getByRole('button', { name: /approve/i })
    fireEvent.click(approve)
    fireEvent.click(approve)
    fireEvent.click(screen.getByRole('button', { name: /deny/i }))
    await waitFor(() => expect(onDecide).toHaveBeenCalledTimes(1))
  })
})

describe('untrusted text', () => {
  it('renders copy as text, never as markup', () => {
    // Copy arrives from a chat request. The server refuses markup, but the
    // card must not be the thing relying on that.
    const { container } = render(
      <OverlayDecisionCard
        proposal={{ ...PROPOSAL, tokens: { 'copy.home.title': '<img src=x onerror=alert(1)>' } }}
        documentVersion={1}
        onDecide={vi.fn()}
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('<img src=x onerror=alert(1)>')
  })

  it('exposes invisible and directional characters instead of rendering them', () => {
    const { container } = render(
      <OverlayDecisionCard
        proposal={{ ...PROPOSAL, tokens: { 'copy.home.title': 'Save‮test' } }}
        documentVersion={1}
        onDecide={vi.fn()}
      />,
    )
    expect(container.textContent).toContain('\\u202E')
    expect(container.textContent).not.toContain('‮')
  })

  it('shows a colour literal beside its swatch', () => {
    // A swatch alone is how a low-contrast pair passes a human check.
    setup()
    expect(screen.getByText('#ffffff')).toBeTruthy()
  })
})

describe('failure and empty states', () => {
  it('keeps the card open when the decision fails', async () => {
    const onDecide = vi.fn().mockRejectedValue(new Error('network down'))
    setup({ onDecide })
    fireEvent.click(screen.getByRole('button', { name: /approve/i }))
    // Clearing the card would tell the operator it landed when it did not.
    await waitFor(() => expect(screen.getByText(/network down/i)).toBeTruthy())
    expect(screen.getByRole('button', { name: /approve/i })).toBeTruthy()
  })

  it('cannot approve an empty proposal, but can still deny it', () => {
    setup({ proposal: { ...PROPOSAL, tokens: {} } })
    expect(screen.getByRole('button', { name: /approve/i }).disabled).toBe(true)
    expect(screen.getByRole('button', { name: /deny/i }).disabled).toBe(false)
  })

  it('renders nothing without a proposal', () => {
    const { container } = render(
      <OverlayDecisionCard proposal={null} documentVersion={1} onDecide={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })
})

describe('style sinks', () => {
  it('never puts a non-canonical colour into a style sink', () => {
    // THE review finding: `url(...)` reached style.background, so the
    // OPERATOR's browser issued the attacker's request while they were
    // deciding whether to approve it.
    const { container } = render(
      <OverlayDecisionCard
        proposal={{ ...PROPOSAL, tokens: { 'color.canvas.bg': 'url(https://attacker.example/track)' } }}
        documentVersion={1}
        onDecide={vi.fn()}
      />,
    )
    expect(container.querySelector('.overlay-swatch')).toBeNull()
    expect(container.innerHTML).not.toContain('attacker.example/track"')
    // Still SHOWN, as text, so the operator can see what was requested.
    expect(container.textContent).toContain('url(https://attacker.example/track)')
  })

  it('still renders a swatch for a canonical colour', () => {
    const { container } = render(
      <OverlayDecisionCard
        proposal={{ ...PROPOSAL, tokens: { 'color.canvas.bg': '#abcdef' } }}
        documentVersion={1}
        onDecide={vi.fn()}
      />,
    )
    expect(container.querySelector('.overlay-swatch')).toBeTruthy()
  })
})
