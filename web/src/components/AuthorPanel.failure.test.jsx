import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import AuthorPanel from './AuthorPanel.jsx'

afterEach(cleanup)

const changeSetId = '24b18e9d-3aaa-4e75-b443-6cab404ffd4c'
const description = 'Validate campaign host enrollment evidence.'

function renderFailure({ pollUrl = `/api/author/stages/${changeSetId}` } = {}) {
  const onAuthor = vi.fn(async () => null)
  const checkStatus = vi.fn()
  const error = Object.assign(new Error('Authoring failed. Your request is saved for recovery.'), { authorTerminal: true, status: 503 })
  const pointer = { description, terminal_failed: true, idempotency_key: 'original-key', change_set_id: changeSetId, poll_url: pollUrl }
  const stageActivity = {
    active: false, phase: 'failed', error, pointer, checkStatus,
    failedRequest: { ...pointer, failure: { reason_code: 'customization_author_job_failed' } },
  }
  render(<AuthorPanel onAuthor={onAuthor} stageActivity={stageActivity} />)
  return { onAuthor, checkStatus }
}

describe('failed author request controls', () => {
  it('shows the request and restores its description without claiming zero charges', () => {
    const { onAuthor } = renderFailure()
    expect(screen.getByTestId('author-failed-request')).toHaveTextContent(changeSetId)
    expect(screen.getByLabelText('What should the tool do?')).toHaveValue(description)
    expect(screen.getByRole('button', { name: 'Generate tool' })).toBeEnabled()
    expect(screen.queryByText(/Nothing was charged/)).not.toBeInTheDocument()
    expect(onAuthor).not.toHaveBeenCalled()
  })

  it('checks status without generating another tool or triggering the retry shortcut', () => {
    const { onAuthor, checkStatus } = renderFailure()
    fireEvent.click(screen.getByTestId('author-failed-check'))
    fireEvent.keyDown(document, { key: 'R' })
    expect(checkStatus).toHaveBeenCalledTimes(1)
    expect(onAuthor).not.toHaveBeenCalled()
  })

  it('makes a new attempt explicit', () => {
    const { onAuthor } = renderFailure()
    fireEvent.click(screen.getByTestId('author-failed-new-attempt'))
    expect(onAuthor).toHaveBeenCalledWith(description, null, { allowSecretOnce: false, newAttempt: true })
  })

  it('does not offer a status request without an accepted poll URL', () => {
    renderFailure({ pollUrl: null })
    expect(screen.getByTestId('author-failed-check')).toBeDisabled()
  })
})
