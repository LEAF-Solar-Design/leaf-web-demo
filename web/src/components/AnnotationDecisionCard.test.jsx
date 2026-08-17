import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import AnnotationDecisionCard from './AnnotationDecisionCard.jsx'

afterEach(cleanup)

const annotation = (over = {}) => ({
  decisionCopy: 'Review <strong>two</strong> annotation changes.',
  batchId: 'batch-1', revision: 2, state: 'pending', kind: 'apply',
  targetVersion: 8, targetCommit: 'a'.repeat(40), previewCommit: 'b'.repeat(40),
  retryOfBatchId: null,
  ...over,
})

const setup = (over = {}) => {
  const handlers = {
    onPreview: vi.fn(), onAccept: vi.fn(), onReject: vi.fn(),
    onRetry: vi.fn(), onUndo: vi.fn(),
  }
  const view = render(
    <AnnotationDecisionCard
      annotation={over.annotation === undefined ? annotation() : over.annotation}
      busy={over.busy}
      error={over.error}
      confirmation={over.confirmation}
      {...handlers}
    />,
  )
  return { ...view, ...handlers }
}

describe('annotation decisions', () => {
  it('renders server copy as text and exposes only state-valid controls', () => {
    const { container, onAccept, onReject } = setup()
    expect(container.querySelector('strong')).toBeNull()
    expect(container.textContent).toContain('<strong>two</strong>')
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    expect(onAccept).toHaveBeenCalledOnce()
    expect(onReject).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /undo/i })).toBeNull()
  })

  it('keeps the card, latches controls, and displays confirmed retry and undo witnesses', () => {
    const { rerender } = setup({
      annotation: annotation({ state: 'rejected' }),
      busy: true,
      error: 'Annotation action did not complete. Nothing changed.',
      confirmation: annotation({
        batchId: 'batch-fresh', revision: 3, retryOfBatchId: 'batch-1',
        previewCommit: 'c'.repeat(40),
      }),
    })
    expect(screen.getByRole('button', { name: /retry as a fresh proposal/i }).disabled).toBe(true)
    expect(screen.getByRole('alert').textContent).toMatch(/nothing changed/i)
    expect(screen.getByText(/fresh proposal batch-fresh, revision 3/i)).toBeTruthy()

    rerender(
      <AnnotationDecisionCard
        annotation={annotation({ state: 'accepted' })}
        confirmation={annotation({
          kind: 'undo', revision: 4, targetVersion: 9, targetCommit: 'd'.repeat(40),
        })}
        onPreview={vi.fn()} onAccept={vi.fn()} onReject={vi.fn()}
        onRetry={vi.fn()} onUndo={vi.fn()}
      />,
    )
    expect(screen.getByRole('button', { name: /prepare undo/i })).toBeTruthy()
    expect(screen.getByText(/inverse confirmed at revision 4, target version 9/i)).toBeTruthy()
  })

  it('renders nothing without an authoritative projection', () => {
    const { container } = setup({ annotation: null })
    expect(container.firstChild).toBeNull()
  })
})
