/**
 * ApprovalCard. Acceptance #2 (keyboard/focus/confirm) and #3 (renders from
 * server truth, refuses on any missing field) each get a named test.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import ApprovalCard from './ApprovalCard.jsx'
import { fieldLabel, normalizeApproval } from './approvalDigest.js'

afterEach(cleanup)

const COMPLETE_RESPONSE = {
  authority_id: 'opauth-1', action: 'operator.worker_credential_rotate',
  credential_handle: 'staging-broker-token', scope: 'staging:broker',
  environment: 'staging', expires_at: '2026-08-17T12:05:00Z',
  reversal_action: 'operator.worker_credential_rotate',
  cost: 'none', args_hash: 'a'.repeat(64),
}

const INCOMPLETE_RESPONSE = {
  authority_id: 'opauth-2', action: 'operator.tenant_agent_pause',
  tenant_id: 'acme-solar', reversal_action: 'operator.tenant_agent_resume',
  expires_at: '2026-08-17T12:05:00Z',
}

function setup(response, over = {}) {
  const { action, authorityId, digest, missing } = normalizeApproval(response, over.sessionEnvironment ?? 'staging')
  const onExecute = over.onExecute || vi.fn().mockResolvedValue(undefined)
  const rendered = render(
    <ApprovalCard
      action={action}
      authorityId={authorityId}
      digest={digest}
      missing={missing}
      fieldLabel={fieldLabel}
      onExecute={onExecute}
      busy={over.busy}
    />,
  )
  return { onExecute, container: rendered.container }
}

describe('acceptance #3: renders server truth, refuses on any missing field', () => {
  it('renders target, environment, cost, scope, expiry, reversal, and argument digest', () => {
    setup(COMPLETE_RESPONSE)
    expect(screen.getByText('credential:staging-broker-token')).toBeTruthy()
    // 'staging' now also renders as the header's environment .tag chip
    // (round-2 panel: blast radius at a glance), so this is no longer the
    // string's only occurrence — pin it to the field digest's <code>, same
    // disambiguation the action/reversal assertion below already needed.
    expect(screen.getByText('staging', { selector: 'code' })).toBeTruthy()
    expect(screen.getByText('none')).toBeTruthy()
    expect(screen.getByText('staging:broker')).toBeTruthy()
    expect(screen.getByText('2026-08-17T12:05:00Z')).toBeTruthy()
    expect(screen.getByText('operator.worker_credential_rotate', { selector: 'code' })).toBeTruthy()
    expect(screen.getByText('a'.repeat(64))).toBeTruthy()
  })

  it('offers Review & Execute when every field is present', () => {
    setup(COMPLETE_RESPONSE)
    expect(screen.getByRole('button', { name: /review & execute/i })).toBeTruthy()
  })

  it('refuses to render an Execute control when a field is missing, and names it', () => {
    setup(INCOMPLETE_RESPONSE)
    expect(screen.queryByRole('button', { name: /review & execute/i })).toBeNull()
    const notice = screen.getByRole('status')
    expect(notice.textContent).toMatch(/missing/i)
    expect(notice.textContent).toMatch(/cost/i)
    expect(notice.textContent).toMatch(/scope/i)
    expect(notice.textContent).toMatch(/argument digest/i)
  })

  it('shows "missing" markers for the specific absent fields, never a fabricated value', () => {
    setup(INCOMPLETE_RESPONSE)
    const missingMarkers = screen.getAllByText('missing')
    expect(missingMarkers.length).toBeGreaterThanOrEqual(3) // cost, scope, argsDigest
  })

  it('renders nothing without a resolvable action/authority', () => {
    const { container } = render(
      <ApprovalCard action={undefined} authorityId={undefined} digest={{}} missing={[]} onExecute={vi.fn()} />,
    )
    expect(container.firstChild).toBeNull()
  })
})

describe('acceptance #2: confirm-before-execute and focus return', () => {
  it('requires a second, explicit confirm before calling onExecute', async () => {
    const { onExecute } = setup(COMPLETE_RESPONSE)
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    expect(onExecute).not.toHaveBeenCalled()
    const dialog = screen.getByRole('alertdialog')
    expect(dialog).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /confirm execute/i }))
    await waitFor(() => expect(onExecute).toHaveBeenCalledWith('opauth-1'))
  })

  it('the confirm dialog has an accessible name naming the action and target', () => {
    setup(COMPLETE_RESPONSE)
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    const dialog = screen.getByRole('alertdialog')
    expect(dialog.getAttribute('aria-label')).toMatch(/operator\.worker_credential_rotate/)
    expect(dialog.getAttribute('aria-label')).toMatch(/credential:staging-broker-token/)
  })

  it('Cancel returns focus to the Execute control that opened the dialog', () => {
    setup(COMPLETE_RESPONSE)
    const executeButton = screen.getByRole('button', { name: /review & execute/i })
    fireEvent.click(executeButton)
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }))
    expect(document.activeElement).toBe(executeButton)
  })

  it('keeps the confirm dialog open, focus inside it, and lets the operator retry on a failed execute', async () => {
    const onExecute = vi.fn().mockRejectedValue(new Error('network down'))
    setup(COMPLETE_RESPONSE, { onExecute })
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    const confirmButton = screen.getByRole('button', { name: /confirm execute/i })
    fireEvent.click(confirmButton)
    await waitFor(() => expect(screen.getByText(/network down/i)).toBeTruthy())
    // Did NOT tell the operator it landed: dialog is still open.
    expect(screen.getByRole('alertdialog')).toBeTruthy()
    // Focus never left the dialog (no strand-outside-the-dialog on failure).
    expect(screen.getByRole('alertdialog').contains(document.activeElement)).toBe(true)
    // Retry is possible: the control is enabled again after the failed attempt settles.
    expect(confirmButton.disabled).toBe(false)
    fireEvent.click(confirmButton)
    await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(2))
  })

  it('a rapid double-click on Confirm cannot fire two executes', async () => {
    const onExecute = vi.fn(() => new Promise(() => {})) // never settles
    setup(COMPLETE_RESPONSE, { onExecute })
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    const confirmButton = screen.getByRole('button', { name: /confirm execute/i })
    fireEvent.click(confirmButton)
    fireEvent.click(confirmButton)
    await waitFor(() => expect(onExecute).toHaveBeenCalledTimes(1))
  })

  it('every actionable control has an accessible name (keyboard/screen-reader reachable)', () => {
    setup(COMPLETE_RESPONSE)
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent?.trim().length || button.getAttribute('aria-label')?.length).toBeTruthy()
    }
  })
})

describe('acceptance #1: no secret-shaped surface', () => {
  it('renders no text input at all', () => {
    const { container } = setup(COMPLETE_RESPONSE)
    expect(container.querySelectorAll('input, textarea').length).toBe(0)
  })
})
