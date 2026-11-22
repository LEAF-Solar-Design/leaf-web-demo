import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import AuthorPanel from './AuthorPanel.jsx'
import useAuthorStageController from '../controllers/useAuthorStageController.js'
import { errorPresentation } from '../errorPresentation.js'

afterEach(cleanup)

const changeSetId = '24b18e9d-3aaa-4e75-b443-6cab404ffd4c'
const description = 'Validate campaign host enrollment evidence.'
const protectedFailureMessage = 'Protected tool authoring could not complete. The approved request was not executed.'

function protectedFailure(reasonCode) {
  return Object.assign(new Error('Internal exception text'), {
    status: 400,
    body: {
      error: { error_code: 'BAD_PARAMS', message: protectedFailureMessage, retryable: false },
      ...(reasonCode === undefined ? {} : { reason_code: reasonCode }),
      diagnostic: 'Private server diagnostic',
      exception: 'Private exception details',
      description: 'Private request description',
      credentials: 'Private credential value',
    },
  })
}

describe('protected authoring failure reasons', () => {
  it.each(['customization_policy_rejected', 'customization_revision_unavailable'])(
    'shows %s beside the safe message after submission', async (reasonCode) => {
      const error = protectedFailure(reasonCode)
      const onAuthor = vi.fn(async () => { throw error })
      const { container } = render(<AuthorPanel onAuthor={onAuthor} />)
      fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })
      await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })) })

      const code = screen.getByText(reasonCode, { selector: 'code' })
      expect(code.parentElement).toHaveTextContent(`Couldn’t author the tool: ${protectedFailureMessage} ${reasonCode}`)
      expect(code.childElementCount).toBe(0)
      expect(screen.queryByText('BAD_PARAMS')).not.toBeInTheDocument()
      for (const privateText of [error.message, error.body.diagnostic, error.body.exception, error.body.description, error.body.credentials]) {
        expect(container).not.toHaveTextContent(privateText)
      }
    },
  )

  it.each([undefined, '', null, { message: 'Private reason details' }, 'Internal exception: private details', '<img src=x onerror=alert(1)>'])(
    'keeps the generic code for a missing or invalid reason %j', (reasonCode) => {
      const { container } = render(<AuthorPanel onAuthor={vi.fn()} stageActivity={{ error: protectedFailure(reasonCode) }} />)
      const code = screen.getByText('BAD_PARAMS', { selector: 'code' })
      expect(code.parentElement).toHaveTextContent(`Couldn’t author the tool: ${protectedFailureMessage} BAD_PARAMS`)
      expect(container.querySelector('img')).toBeNull()
      expect(container).not.toHaveTextContent('Private reason details')
      expect(container).not.toHaveTextContent('Internal exception: private details')
    },
  )

  it('updates the displayed reason when the same safe message accompanies a new failure', () => {
    const props = { onAuthor: vi.fn() }
    const view = render(<AuthorPanel {...props} stageActivity={{ error: protectedFailure('customization_policy_rejected') }} />)
    view.rerender(<AuthorPanel {...props} stageActivity={{ error: protectedFailure('customization_revision_unavailable') }} />)
    expect(screen.getByText('customization_revision_unavailable', { selector: 'code' })).toBeInTheDocument()
    expect(screen.queryByText('customization_policy_rejected')).not.toBeInTheDocument()
    expect(screen.getByText(`Couldn’t author the tool: ${protectedFailureMessage}`)).toBeInTheDocument()
  })

  it('preserves shared formatter behavior when a caller does not supply the reason code', () => {
    expect(errorPresentation(protectedFailure('customization_policy_rejected'))).toEqual({
      message: protectedFailureMessage,
      code: 'BAD_PARAMS',
      nextAction: '',
      actor: '',
      retryable: false,
    })
    expect(errorPresentation(new Error('Connection lost'))).toMatchObject({ message: 'Connection lost', code: '' })
  })
})

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
  it('preserves the signed-out demo description without Retry or Resume controls', async () => {
    const authorityProvider = vi.fn(async () => null)
    const stageAuthorTool = vi.fn()
    function DemoAuthor() {
      const controller = useAuthorStageController({ mock: true, authorityProvider, stageAuthorTool })
      return <AuthorPanel onAuthor={controller.stage} stageActivity={controller} />
    }
    render(<DemoAuthor />)
    fireEvent.change(screen.getByLabelText('What should the tool do?'), { target: { value: description } })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Generate tool' })) })
    expect(screen.getByText(/Tool building is unavailable in this signed-out demo\./)).toBeInTheDocument()
    expect(screen.getByText('Your description is preserved.')).toBeInTheDocument()
    expect(screen.getByLabelText('What should the tool do?')).toHaveValue(description)
    expect(screen.queryByRole('button', { name: /Retry|Resume/i })).not.toBeInTheDocument()
    fireEvent.keyDown(document, { key: 'R' })
    expect(authorityProvider).toHaveBeenCalledTimes(1)
    expect(stageAuthorTool).not.toHaveBeenCalled()
  })

  it.each([false, true])('suppresses the signed-out control even when resumable is %s', (resumable) => {
    const error = Object.assign(new Error('Tool building is unavailable in this signed-out demo.'), {
      reasonCode: 'signed-out-demo',
    })
    render(<AuthorPanel onAuthor={vi.fn()} stageActivity={{ error, resumable }} onResumeAuthor={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Retry|Resume/i })).not.toBeInTheDocument()
    expect(screen.getByText('Your description is preserved.')).toBeInTheDocument()
  })

  it.each([false, true])('keeps the existing recovery control for other errors when resumable is %s', (resumable) => {
    render(<AuthorPanel onAuthor={vi.fn()} stageActivity={{ error: new Error('Connection lost'), resumable }} onResumeAuthor={vi.fn()} />)
    expect(screen.getByRole('button', { name: resumable ? 'Resume authoring' : /Retry/ })).toBeInTheDocument()
  })

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
