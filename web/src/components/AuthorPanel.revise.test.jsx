import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import AuthorPanel from './AuthorPanel.jsx'

afterEach(cleanup)

const seed = 'Count panels'
const revisedSeed = 'Count selected panels'
const pointer = { idempotency_key: 'request-prepared', description: seed, draft_only: true }
const field = () => screen.getByLabelText('What should the tool do?')

describe('author revision seed and cancellation', () => {
  it('W21D1B-seed-prepared', () => {
    const onAuthor = vi.fn()
    const props = { onAuthor, seedAutoSubmit: true, stageActivity: { pointer, draftOnly: true } }
    const view = render(<AuthorPanel {...props} seed={seed} seedSignal={1} />)
    expect(field()).toHaveValue(seed)
    expect(onAuthor).not.toHaveBeenCalled()
    view.rerender(<AuthorPanel {...props} seed={revisedSeed} seedSignal={2} />)
    expect(field()).toHaveValue(revisedSeed)
    expect(field()).toBeEnabled()
    expect(onAuthor).not.toHaveBeenCalled()
  })

  it('W21D1B-seed-revise', () => {
    const onAuthor = vi.fn()
    const props = { onAuthor, seedAutoSubmit: true, stageActivity: { pointer, draftOnly: false } }
    const view = render(<AuthorPanel {...props} seed={seed} seedSignal={1} />)
    view.rerender(<AuthorPanel {...props} seed={revisedSeed} seedSignal={2} />)
    expect(field()).toHaveValue(revisedSeed)
    expect(onAuthor).not.toHaveBeenCalled()
  })

  it('W21D1B-cancel-revision-disabled', () => {
    const onCancelRevision = vi.fn()
    const props = { onAuthor: vi.fn(), targetToolName: 'panel_counter', onCancelRevision }
    const view = render(<AuthorPanel {...props} stageActivity={{ pointer, draftOnly: true }} />)
    const cancel = () => screen.getByRole('button', { name: 'Cancel revision' })
    expect(cancel()).toBeDisabled()
    expect(cancel()).toHaveAttribute('title', 'Finish or discard the current request before cancelling the revision.')
    fireEvent.click(cancel())
    expect(onCancelRevision).not.toHaveBeenCalled()
    view.rerender(<AuthorPanel {...props} stageActivity={{ pointer: null, draftOnly: false }} />)
    expect(cancel()).toBeEnabled()
    expect(cancel()).not.toHaveAttribute('title')
    fireEvent.click(cancel())
    expect(onCancelRevision).toHaveBeenCalledTimes(1)
  })
})
