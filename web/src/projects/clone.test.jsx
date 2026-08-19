/**
 * CloneDialog. Card B-U3 acceptance oracle, one describe block per clause:
 *   Clone produces a new project owned by the cloner; progress and failure
 *   states surfaced; receipt id shown on completion.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import CloneDialog from './CloneDialog.jsx'

afterEach(cleanup)

const PROJECT = { project_id: 'proj-1', name: 'Rooftop Array' }
const RECEIPT = {
  receipt_id: 'rcpt-abc123',
  project_id: 'proj-2',
  name: 'Rooftop Array (copy)',
  owner_id: 'u-cloner',
  owner_name: 'Cloner One',
}

function setup(over = {}) {
  const onClone = over.onClone || vi.fn().mockResolvedValue(RECEIPT)
  const onClose = over.onClose || vi.fn()
  const rendered = render(
    <CloneDialog
      open={over.open ?? true}
      project={over.project ?? PROJECT}
      onClone={onClone}
      onClose={onClose}
    />,
  )
  return { onClone, onClose, ...rendered }
}

describe('acceptance: clone produces a new project owned by the cloner', () => {
  it('calls onClone with the source project id when the operator starts the clone', async () => {
    const { onClone } = setup()
    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))
    await waitFor(() => expect(onClone).toHaveBeenCalledWith('proj-1'))
  })

  it("shows the new project as owned by the cloner, read from the server's receipt", async () => {
    setup()
    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))
    await waitFor(() => {
      expect(screen.getByText(/owned by cloner one/i)).toBeTruthy()
    })
  })

  it('renders nothing when there is no open dialog or no project to clone', () => {
    const { container } = render(
      <CloneDialog open={false} project={PROJECT} onClone={vi.fn()} onClose={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()

    cleanup()

    const { container: container2 } = render(
      <CloneDialog open={true} project={null} onClone={vi.fn()} onClose={vi.fn()} />,
    )
    expect(container2.firstChild).toBeNull()
  })
})

describe('acceptance: progress and failure states are surfaced', () => {
  it('shows an in-progress status while the clone is running and disables closing', async () => {
    let resolveClone
    const onClone = vi.fn(() => new Promise((resolve) => { resolveClone = resolve }))
    setup({ onClone })

    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))

    await waitFor(() => {
      expect(screen.getByRole('status').textContent).toMatch(/cloning/i)
    })
    expect(screen.getByRole('button', { name: /close/i })).toBeDisabled()

    resolveClone(RECEIPT)
    await waitFor(() => expect(screen.getByText(/receipt/i)).toBeTruthy())
  })

  it('surfaces a failure state with the server error and offers a retry, without losing the project context', async () => {
    const onClone = vi.fn().mockRejectedValue({ body: { detail: 'Clone quota exceeded.' } })
    setup({ onClone })

    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))

    await waitFor(() => {
      expect(screen.getByRole('alert').textContent).toMatch(/clone quota exceeded/i)
    })
    expect(screen.getByRole('button', { name: /retry clone/i })).toBeTruthy()
  })

  it('lets the operator retry after a failure, calling onClone again', async () => {
    const onClone = vi.fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(RECEIPT)
    setup({ onClone })

    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /retry clone/i }))
    await waitFor(() => expect(onClone).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getByText(/receipt/i)).toBeTruthy())
  })
})

describe('acceptance: receipt id shown on completion', () => {
  it("displays the server's receipt id once the clone completes", async () => {
    setup()
    fireEvent.click(screen.getByRole('button', { name: /^clone project$/i }))
    await waitFor(() => {
      expect(screen.getByText('rcpt-abc123')).toBeTruthy()
    })
  })

  it('shows no receipt id before the clone has completed', () => {
    setup()
    expect(screen.queryByText(/receipt:/i)).toBeNull()
  })
})
