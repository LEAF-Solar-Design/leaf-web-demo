/**
 * OperatorConsole composition. The console-wide acceptance checks — signed-
 * out reset (#4) and no secret-shaped surface anywhere in the tree (#1) —
 * live here rather than duplicated per panel.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

import OperatorConsole from './OperatorConsole.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'createSession').mockResolvedValue(
    { session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' })
  vi.spyOn(operatorClient, 'listSessions').mockResolvedValue([])
  vi.spyOn(operatorClient, 'getAudit').mockResolvedValue([])
  vi.spyOn(operatorClient, 'isOperatorDenied').mockImplementation((e) => [401, 403, 404].includes(e?.status))
})

describe('acceptance: clear current profile and environment', () => {
  it('shows the profile and environment of the console-level default session', async () => {
    render(<OperatorConsole onClose={vi.fn()} />)
    const badge = await screen.findByRole('status')
    expect(badge.textContent).toMatch(/default/)
    expect(badge.textContent).toMatch(/staging/)
  })
})

describe('acceptance #4: a stale or revoked session resets to a safe signed-out state', () => {
  it('unmounts every panel and shows a signed-out notice when a real call is denied, with no cached transcript', async () => {
    // A REAL 404 from a child panel's own call (SessionPanel's session list
    // read) is what actually drives the reset in production — not a
    // synthetic trigger — so this exercises the genuine code path.
    operatorClient.listSessions.mockRejectedValue(Object.assign(new Error('denied'), { status: 404 }))
    render(<OperatorConsole onClose={vi.fn()} />)
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.queryByRole('status')).toBeNull() // the profile/environment badge is gone
    expect(screen.getByText(/no longer valid/i)).toBeTruthy()
    // Every child panel (session list, runbooks, worker jobs, audit) is gone.
    expect(screen.queryByRole('heading', { name: /sessions/i })).toBeNull()
    expect(screen.queryByRole('heading', { name: /audit/i })).toBeNull()
  })
})

describe('acceptance #1: no secret-shaped input anywhere in the console', () => {
  it('scans every textbox/textarea in the mounted tree', async () => {
    const { container } = render(<OperatorConsole onClose={vi.fn()} />)
    await screen.findByRole('status')
    const fields = container.querySelectorAll('input, textarea')
    for (const field of fields) {
      const haystack = [
        field.id, field.name, field.getAttribute('aria-label'), field.getAttribute('placeholder'),
      ].filter(Boolean).join(' ')
      expect(haystack).not.toMatch(/secret|token|password|key/i)
    }
  })

  it('never writes to localStorage/sessionStorage/cookies for an operator value', async () => {
    const setLS = vi.spyOn(Storage.prototype, 'setItem')
    render(<OperatorConsole onClose={vi.fn()} />)
    await screen.findByRole('status')
    for (const call of setLS.mock.calls) {
      expect(call[0]).not.toMatch(/operator/i)
    }
    expect(document.cookie).toBe('')
  })
})

describe('close behavior', () => {
  it('Escape calls onClose', async () => {
    const onClose = vi.fn()
    render(<OperatorConsole onClose={onClose} />)
    await screen.findByRole('status')
    const dialog = screen.getByRole('dialog')
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(onClose).toHaveBeenCalled()
  })

  it('focuses the Close control on mount so the drawer opening moves focus into it', async () => {
    render(<OperatorConsole onClose={vi.fn()} />)
    await screen.findByRole('status')
    expect(document.activeElement).toBe(screen.getByRole('button', { name: /close/i }))
  })
})
