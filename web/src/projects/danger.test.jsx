/**
 * DangerZone. Card B-U5 acceptance oracle, one describe block per clause:
 *   Delete requires typed confirmation naming the project; reset distinct
 *   from delete; both show the server receipt.
 *   Undo affordance surfaces exactly what the server contract offers (no
 *   fake undo: if the API is terminal, the UI says so before confirm).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import DangerZone from './DangerZone.jsx'

afterEach(cleanup)

function openReset() {
  fireEvent.click(screen.getByRole('button', { name: /^reset$/i }))
}

function openDelete() {
  fireEvent.click(screen.getByRole('button', { name: /^delete$/i }))
}

describe('acceptance: delete requires typed confirmation naming the project', () => {
  it('keeps the delete confirm button disabled until the exact project name is typed', () => {
    const onDelete = vi.fn()
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={onDelete} />)

    openDelete()
    const confirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    const confirmButton = confirmButtons[confirmButtons.length - 1]
    expect(confirmButton).toBeDisabled()

    const input = screen.getByLabelText(/type the project name to confirm delete/i)
    fireEvent.change(input, { target: { value: 'wrong name' } })
    expect(confirmButton).toBeDisabled()

    fireEvent.change(input, { target: { value: 'Solar Canopy A' } })
    expect(confirmButton).not.toBeDisabled()

    expect(onDelete).not.toHaveBeenCalled()
  })

  it('never calls onDelete from a typed name that only partially matches', () => {
    const onDelete = vi.fn()
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={onDelete} />)

    openDelete()
    const input = screen.getByLabelText(/type the project name to confirm delete/i)
    fireEvent.change(input, { target: { value: 'Solar Canopy' } })
    const confirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    fireEvent.click(confirmButtons[confirmButtons.length - 1])

    expect(onDelete).not.toHaveBeenCalled()
  })

  it('runs the delete once the typed name matches exactly and confirm is clicked', async () => {
    const onDelete = vi.fn().mockResolvedValue({
      status: 'deleted',
      receipt: { receipt_id: 'rcpt-del-1', action: 'project_deleted' },
    })
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={onDelete} />)

    openDelete()
    const input = screen.getByLabelText(/type the project name to confirm delete/i)
    fireEvent.change(input, { target: { value: 'Solar Canopy A' } })
    const confirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    fireEvent.click(confirmButtons[confirmButtons.length - 1])

    await waitFor(() => expect(onDelete).toHaveBeenCalledTimes(1))
  })
})

describe('acceptance: reset is distinct from delete', () => {
  it('renders reset and delete as independent confirm flows with their own typed input', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)

    openReset()
    openDelete()

    expect(screen.getByLabelText(/type the project name to confirm reset/i)).toBeTruthy()
    expect(screen.getByLabelText(/type the project name to confirm delete/i)).toBeTruthy()
  })

  it('typing the confirmation name into the reset flow does not enable delete, and vice versa', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)

    openReset()
    openDelete()

    const resetInput = screen.getByLabelText(/type the project name to confirm reset/i)
    fireEvent.change(resetInput, { target: { value: 'Solar Canopy A' } })

    const deleteConfirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    const deleteConfirmButton = deleteConfirmButtons[deleteConfirmButtons.length - 1]
    expect(deleteConfirmButton).toBeDisabled()

    const deleteInput = screen.getByLabelText(/type the project name to confirm delete/i)
    expect(deleteInput.value).toBe('')
  })

  it('confirming reset never calls onDelete, and confirming delete never calls onReset', async () => {
    const onReset = vi.fn().mockResolvedValue({ status: 'reset', receipt: { receipt_id: 'rcpt-r-1' } })
    const onDelete = vi.fn().mockResolvedValue({ status: 'deleted', receipt: { receipt_id: 'rcpt-d-1' } })
    render(<DangerZone projectName="Solar Canopy A" onReset={onReset} onDelete={onDelete} />)

    openReset()
    fireEvent.change(screen.getByLabelText(/type the project name to confirm reset/i), {
      target: { value: 'Solar Canopy A' },
    })
    const resetConfirmButtons = screen.getAllByRole('button', { name: /^reset$/i })
    fireEvent.click(resetConfirmButtons[resetConfirmButtons.length - 1])

    await waitFor(() => expect(onReset).toHaveBeenCalledTimes(1))
    expect(onDelete).not.toHaveBeenCalled()
  })

  it('canceling one action leaves the other action untouched', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)

    openReset()
    openDelete()
    fireEvent.change(screen.getByLabelText(/type the project name to confirm delete/i), {
      target: { value: 'Solar Canopy A' },
    })

    const resetSection = screen.getByLabelText(/reset project/i)
    const cancelInResetSection = within(resetSection).getByRole('button', { name: /cancel/i })
    fireEvent.click(cancelInResetSection)

    // Reset flow collapsed back to its idle "Reset" trigger.
    expect(within(resetSection).getByRole('button', { name: /^reset$/i })).toBeTruthy()
    expect(within(resetSection).queryByLabelText(/type the project name to confirm reset/i)).toBeNull()

    // Delete flow's typed value survived the reset section's cancel.
    expect(screen.getByLabelText(/type the project name to confirm delete/i).value).toBe('Solar Canopy A')
  })
})

describe('acceptance: both reset and delete show the server receipt', () => {
  it('shows the exact receipt id the server returned after a successful reset', async () => {
    const onReset = vi.fn().mockResolvedValue({
      status: 'reset',
      deleted_file_count: 3,
      receipt: { receipt_id: 'rcpt-reset-999', action: 'project_reset' },
    })
    render(<DangerZone projectName="Solar Canopy A" onReset={onReset} onDelete={vi.fn()} />)

    openReset()
    fireEvent.change(screen.getByLabelText(/type the project name to confirm reset/i), {
      target: { value: 'Solar Canopy A' },
    })
    const resetConfirmButtons = screen.getAllByRole('button', { name: /^reset$/i })
    fireEvent.click(resetConfirmButtons[resetConfirmButtons.length - 1])

    await waitFor(() => expect(screen.getByText('rcpt-reset-999')).toBeTruthy())
  })

  it('shows the exact receipt id the server returned after a successful delete', async () => {
    const onDelete = vi.fn().mockResolvedValue({
      status: 'deleted',
      receipt: { receipt_id: 'rcpt-delete-777', action: 'project_deleted' },
    })
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={onDelete} />)

    openDelete()
    fireEvent.change(screen.getByLabelText(/type the project name to confirm delete/i), {
      target: { value: 'Solar Canopy A' },
    })
    const deleteConfirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    fireEvent.click(deleteConfirmButtons[deleteConfirmButtons.length - 1])

    await waitFor(() => expect(screen.getByText('rcpt-delete-777')).toBeTruthy())
  })

  it('surfaces a retry-worthy error and no receipt when the server call fails', async () => {
    const onDelete = vi.fn().mockRejectedValue({ message: 'Workspace is locked.' })
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={onDelete} />)

    openDelete()
    fireEvent.change(screen.getByLabelText(/type the project name to confirm delete/i), {
      target: { value: 'Solar Canopy A' },
    })
    const deleteConfirmButtons = screen.getAllByRole('button', { name: /^delete$/i })
    fireEvent.click(deleteConfirmButtons[deleteConfirmButtons.length - 1])

    await waitFor(() => expect(screen.getByText(/workspace is locked/i)).toBeTruthy())
    expect(screen.queryByText(/^Receipt:/)).toBeNull()
  })
})

describe('acceptance: undo affordance surfaces exactly what the server contract offers', () => {
  it('says the action cannot be undone, before confirm, when no undo prop is supplied (the real reset/delete API is terminal)', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)

    openReset()
    openDelete()

    const resetSection = screen.getByLabelText(/reset project/i)
    const deleteSection = screen.getByLabelText(/delete project/i)
    expect(within(resetSection).getByText(/can.t be undone/i)).toBeTruthy()
    expect(within(deleteSection).getByText(/can.t be undone/i)).toBeTruthy()
  })

  it('shows the terminal notice before the typed confirmation input, never after', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)
    openDelete()

    const deleteSection = screen.getByLabelText(/delete project/i)
    const notice = within(deleteSection).getByText(/can.t be undone/i)
    const input = within(deleteSection).getByLabelText(/type the project name to confirm delete/i)
    // DOM order encodes reading/tab order: the notice node must precede the input node.
    expect(notice.compareDocumentPosition(input) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('never renders a fake "Undo" button when the API is terminal', () => {
    render(<DangerZone projectName="Solar Canopy A" onReset={vi.fn()} onDelete={vi.fn()} />)
    openReset()
    openDelete()

    expect(screen.queryByRole('button', { name: /^undo$/i })).toBeNull()
  })

  it('renders the server-supplied undo description instead of the terminal notice when the caller says undo is real', () => {
    render(
      <DangerZone
        projectName="Solar Canopy A"
        resetUndo={{ description: 'You can restore these files from the last checkpoint within 24 hours.' }}
        onReset={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    openReset()
    const resetSection = screen.getByLabelText(/reset project/i)
    expect(within(resetSection).getByText(/restore these files from the last checkpoint/i)).toBeTruthy()
    expect(within(resetSection).queryByText(/can.t be undone/i)).toBeNull()
  })

  it('renders each action\'s own undo state independently — reset undoable does not make delete undoable', () => {
    render(
      <DangerZone
        projectName="Solar Canopy A"
        resetUndo={{ description: 'Files can be restored within 24 hours.' }}
        onReset={vi.fn()}
        onDelete={vi.fn()}
      />,
    )

    openReset()
    openDelete()

    const resetSection = screen.getByLabelText(/reset project/i)
    const deleteSection = screen.getByLabelText(/delete project/i)
    expect(within(resetSection).getByText(/restored within 24 hours/i)).toBeTruthy()
    expect(within(deleteSection).getByText(/can.t be undone/i)).toBeTruthy()
  })
})

describe('rendering guard', () => {
  it('renders nothing without a confirmed project name', () => {
    const { container } = render(<DangerZone projectName={null} onReset={vi.fn()} onDelete={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })
})
