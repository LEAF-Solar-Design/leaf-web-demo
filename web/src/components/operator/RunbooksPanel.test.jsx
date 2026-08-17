import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import RunbooksPanel from './RunbooksPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'tenantAgentState').mockResolvedValue({ agent_disabled: false, revision: 3 })
  vi.spyOn(operatorClient, 'tenantOverlayState').mockResolvedValue({ overlay: {}, revision: 3 })
  vi.spyOn(operatorClient, 'tenantAgentPropose').mockResolvedValue({
    authority_id: 'opauth-1', action: 'operator.tenant_agent_pause', tenant_id: 'acme-solar',
    target_revision: 3, before: { agent_disabled: false, revision: 3 },
    reversal_action: 'operator.tenant_agent_resume', expires_at: '2026-08-17T12:05:00Z',
  })
  vi.spyOn(operatorClient, 'credentialState').mockResolvedValue({ revision: 1, rotated_at: null })
  vi.spyOn(operatorClient, 'credentialPropose').mockResolvedValue({
    authority_id: 'opauth-2', action: 'operator.worker_credential_rotate',
    credential_handle: 'staging-broker-token', scope: 'staging:broker', environment: 'staging',
    target_revision: 1, before: { revision: 1 }, expires_at: '2026-08-17T12:10:00Z',
  })
  vi.spyOn(operatorClient, 'externalDestinations').mockResolvedValue([])
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

describe('acceptance: fleet and tenant inspection, read-only', () => {
  it('loads tenant-agent and tenant-overlay state from their .../state routes', async () => {
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText(/tenant id/i), { target: { value: 'acme-solar' } })
    fireEvent.click(screen.getByRole('button', { name: /load tenant state/i }))
    await waitFor(() => expect(operatorClient.tenantAgentState).toHaveBeenCalledWith('acme-solar'))
    expect(operatorClient.tenantOverlayState).toHaveBeenCalledWith('acme-solar')
    expect(await screen.findByText('false')).toBeTruthy() // agent_disabled
  })
})

describe('acceptance: approval cards built from server truth', () => {
  it('a tenant-agent pause proposal renders an ApprovalCard from the real propose shape', async () => {
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText(/tenant id/i), { target: { value: 'acme-solar' } })
    fireEvent.click(screen.getByRole('button', { name: /load tenant state/i }))
    fireEvent.click(await screen.findByRole('button', { name: /propose pause/i }))
    // Real server shape from operator_runbooks.py.propose() lacks cost/scope/
    // argsDigest, so the card correctly blocks Execute rather than fabricate them.
    expect(await screen.findByRole('status')).toHaveProperty('textContent',
      expect.stringMatching(/missing/i))
    expect(screen.getByText(/tenant:acme-solar/)).toBeTruthy()
  })

  it('a credential-rotate proposal executes through the runbook when confirmed with scope+environment present', async () => {
    operatorClient.credentialPropose.mockResolvedValue({
      authority_id: 'opauth-3', action: 'operator.worker_credential_rotate',
      credential_handle: 'staging-broker-token', scope: 'staging:broker', environment: 'staging',
      expires_at: '2026-08-17T12:10:00Z', reversal_action: 'operator.worker_credential_rotate',
      cost: 'none', args_hash: 'b'.repeat(64), // simulated complete backend response
    })
    vi.spyOn(operatorClient, 'credentialExecute').mockResolvedValue({ authority_id: 'opauth-3' })
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText(/credential handle/i), { target: { value: 'staging-broker-token' } })
    fireEvent.click(screen.getByRole('button', { name: /load credential state/i }))
    fireEvent.click(await screen.findByRole('button', { name: /propose rotate/i }))
    fireEvent.click(await screen.findByRole('button', { name: /review & execute/i }))
    fireEvent.click(await screen.findByRole('button', { name: /confirm execute/i }))
    await waitFor(() => expect(operatorClient.credentialExecute)
      .toHaveBeenCalledWith('staging-broker-token', 'opauth-3'))
    // The resolved proposal's card is removed once it executes.
    await waitFor(() => expect(screen.queryByRole('button', { name: /confirm execute/i })).toBeNull())
  })
})

describe('acceptance #1: tenant id and credential handle fields are not secret-shaped', () => {
  it('neither input is labeled secret/token/password/key', () => {
    render(<RunbooksPanel sessionEnvironment="staging" />)
    for (const field of screen.getAllByRole('textbox')) {
      expect(field.id || field.getAttribute('aria-label') || '').not.toMatch(/secret|token|password|key/i)
    }
  })
})
