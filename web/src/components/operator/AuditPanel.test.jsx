import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import AuditPanel from './AuditPanel.jsx'
import * as operatorClient from '../../operatorClient.js'
import { MSG_SERVICE } from '../../errorHumanize.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

describe('acceptance: audit and artifact links', () => {
  it('renders audit rows from GET /api/operator/audit with a session link', async () => {
    vi.spyOn(operatorClient, 'getAudit').mockResolvedValue([
      { ts: '2026-08-17T12:00:00Z', session_id: 'opsess-1', turn_id: 't-1', action: 'operator.tenant_agent_pause',
        decision: 'mint', reason: 'authority_minted', authority_id: 'opauth-1' },
    ])
    render(<AuditPanel />)
    expect(await screen.findByText('operator.tenant_agent_pause')).toBeTruthy()
    const link = screen.getByRole('link', { name: /session opsess-1/i })
    expect(link.getAttribute('href')).toContain('opsess-1')
  })

  it('shows an empty state, never a fabricated row', async () => {
    vi.spyOn(operatorClient, 'getAudit').mockResolvedValue([])
    render(<AuditPanel />)
    expect(await screen.findByText(/no audit rows yet/i)).toBeTruthy()
  })
})

describe('acceptance #4: signed-out reset', () => {
  it('calls onSignedOut when the audit read is denied', async () => {
    vi.spyOn(operatorClient, 'getAudit').mockRejectedValue(Object.assign(new Error('denied'), { status: 401 }))
    operatorClient.isOperatorDenied.mockReturnValue(true)
    const onSignedOut = vi.fn()
    render(<AuditPanel onSignedOut={onSignedOut} />)
    await waitFor(() => expect(onSignedOut).toHaveBeenCalled())
  })
})

describe('S1-81: audit error recovery', () => {
  it('clears the previous error as soon as a refresh starts', async () => {
    let resolveRefresh
    const refresh = new Promise((resolve) => { resolveRefresh = resolve })
    vi.spyOn(operatorClient, 'getAudit')
      .mockRejectedValueOnce(new Error('Could not load the audit log.'))
      .mockReturnValueOnce(refresh)
    render(<AuditPanel />)
    expect(await screen.findByRole('alert')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(screen.queryByRole('alert')).toBeNull()
    await act(async () => { resolveRefresh([]) })
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows successful refresh rows without retaining the previous error', async () => {
    vi.spyOn(operatorClient, 'getAudit')
      .mockRejectedValueOnce(new Error('Could not load the audit log.'))
      .mockResolvedValueOnce([
        { ts: '2026-10-25T12:00:00Z', action: 'Pause approved', decision: 'Approved', reason: 'Requested' },
      ])
    render(<AuditPanel />)
    await screen.findByRole('alert')

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Pause approved')).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('humanizes audit transport errors before displaying them', async () => {
    vi.spyOn(operatorClient, 'getAudit').mockRejectedValue(new Error('GET /api/operator/audit -> 500'))
    render(<AuditPanel />)
    expect((await screen.findByRole('alert')).textContent).toBe(MSG_SERVICE)
  })
})
