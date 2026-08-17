import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import AuditPanel from './AuditPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

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
