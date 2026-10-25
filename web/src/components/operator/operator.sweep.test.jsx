import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import SessionPanel from './SessionPanel.jsx'
import RunbooksPanel from './RunbooksPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

const event = (type) => [{ seq: 1, type, data: { nested: { message: type } } }]
const proposal = (fields) => ({
  authority_id: 'approval-a', environment: 'staging', cost: 'none', scope: 'staging',
  expires_at: '2099-01-01T00:00:00Z', reversal_action: 'Restore previous settings',
  args_hash: 'b'.repeat(64), ...fields,
})

beforeEach(() => {
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function renderSessions() {
  vi.spyOn(operatorClient, 'listSessions').mockResolvedValue([
    { session_id: 'session-a', profile: 'Alpha', environment: 'staging', status: 'idle' },
    { session_id: 'session-b', profile: 'Beta', environment: 'staging', status: 'idle' },
  ])
  render(<SessionPanel />)
  await screen.findByRole('button', { name: /Alpha/ })
}

describe('S1-81: transcript selection', () => {
  it('ignores an older read even after returning to the same session', async () => {
    const oldRead = deferred()
    vi.spyOn(operatorClient, 'getEvents')
      .mockReturnValueOnce(oldRead.promise)
      .mockResolvedValueOnce(event('beta-event'))
      .mockResolvedValueOnce(event('latest-alpha-event'))
    await renderSessions()
    fireEvent.click(screen.getByRole('button', { name: /Alpha/ }))
    fireEvent.click(screen.getByRole('button', { name: /Beta/ }))
    await screen.findByText('beta-event')
    fireEvent.click(screen.getByRole('button', { name: /Alpha/ }))
    await screen.findByText('latest-alpha-event')
    await act(async () => { oldRead.resolve(event('stale-alpha-event')) })
    expect(screen.queryByText('stale-alpha-event')).toBeNull()
    expect(screen.getByText('latest-alpha-event')).toBeTruthy()
  })

  it('clears the previous transcript and expansion state when selecting another session', async () => {
    const nextRead = deferred()
    vi.spyOn(operatorClient, 'getEvents')
      .mockResolvedValueOnce(event('alpha-event'))
      .mockReturnValueOnce(nextRead.promise)
    await renderSessions()
    fireEvent.click(screen.getByRole('button', { name: /Alpha/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Show full event data/ }))
    fireEvent.click(screen.getByRole('button', { name: /Beta/ }))
    expect(screen.queryByText('alpha-event')).toBeNull()
    await act(async () => { nextRead.resolve(event('beta-event')) })
    expect(screen.getByRole('button', { name: /Show full event data/ }).getAttribute('aria-expanded')).toBe('false')
  })

  it('keeps a delayed send refresh out of the newly selected transcript', async () => {
    const refresh = deferred()
    vi.spyOn(operatorClient, 'postMessage').mockResolvedValue({})
    vi.spyOn(operatorClient, 'getEvents')
      .mockResolvedValueOnce(event('alpha-event'))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(event('beta-event'))
    await renderSessions()
    fireEvent.click(screen.getByRole('button', { name: /Alpha/ }))
    await screen.findByText('alpha-event')
    fireEvent.change(screen.getByLabelText('Message'), { target: { value: 'Hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(operatorClient.getEvents).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: /Beta/ }))
    await screen.findByText('beta-event')
    await act(async () => { refresh.resolve(event('alpha-reply')) })
    expect(operatorClient.postMessage).toHaveBeenCalledWith('session-a', 'Hello')
    expect(screen.queryByText('alpha-reply')).toBeNull()
    expect(screen.getByText('beta-event')).toBeTruthy()
  })

  it('ignores errors from an obsolete transcript request', async () => {
    const oldRead = deferred()
    vi.spyOn(operatorClient, 'getEvents')
      .mockReturnValueOnce(oldRead.promise)
      .mockResolvedValueOnce(event('beta-event'))
    await renderSessions()
    fireEvent.click(screen.getByRole('button', { name: /Alpha/ }))
    fireEvent.click(screen.getByRole('button', { name: /Beta/ }))
    await screen.findByText('beta-event')
    await act(async () => { oldRead.reject(new Error('Could not load the earlier session.')) })
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('S1-214: proposal targets', () => {
  it('executes a delayed tenant proposal against its original tenant and clears old readouts', async () => {
    const pending = deferred()
    vi.spyOn(operatorClient, 'tenantAgentState').mockResolvedValue({ agent_disabled: false, revision: 3 })
    vi.spyOn(operatorClient, 'tenantOverlayState').mockResolvedValue({ revision: 3 })
    vi.spyOn(operatorClient, 'tenantAgentPropose').mockReturnValue(pending.promise)
    vi.spyOn(operatorClient, 'tenantAgentExecute').mockResolvedValue({})
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tenant-a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load tenant state' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Propose pause' }))
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tenant-b' } })
    expect(screen.queryByText('false')).toBeNull()
    expect(screen.queryByText('overlay revision')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Propose pause' })).toBeNull()
    await act(async () => {
      pending.resolve(proposal({ action: 'operator.tenant_agent_pause', tenant_id: 'tenant-a' }))
    })
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    fireEvent.click(screen.getByRole('button', { name: /confirm execute/i }))
    await waitFor(() => expect(operatorClient.tenantAgentExecute).toHaveBeenCalledWith('pause', 'tenant-a', 'approval-a'))
    await waitFor(() => expect(screen.queryByRole('group')).toBeNull())
    expect(operatorClient.tenantAgentState).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('false')).toBeNull()
  })

  it('executes a credential proposal against its original handle after the field changes', async () => {
    vi.spyOn(operatorClient, 'credentialState').mockResolvedValue({ revision: 7, rotated_at: 'Yesterday' })
    vi.spyOn(operatorClient, 'credentialPropose').mockResolvedValue(proposal({
      action: 'operator.worker_credential_rotate', credential_handle: 'handle-a',
    }))
    vi.spyOn(operatorClient, 'credentialExecute').mockResolvedValue({})
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText('Credential handle'), { target: { value: 'handle-a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load credential state' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Propose rotate' }))
    await screen.findByRole('button', { name: /review & execute/i })
    fireEvent.change(screen.getByLabelText('Credential handle'), { target: { value: 'handle-b' } })
    expect(screen.queryByText('Yesterday')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Propose rotate' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /review & execute/i }))
    fireEvent.click(screen.getByRole('button', { name: /confirm execute/i }))
    await waitFor(() => expect(operatorClient.credentialExecute).toHaveBeenCalledWith('handle-a', 'approval-a'))
    await waitFor(() => expect(screen.queryByRole('group')).toBeNull())
    expect(operatorClient.credentialState).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Yesterday')).toBeNull()
  })

  it('does not restore stale readouts when pending reads finish after the inputs change', async () => {
    const tenant = deferred()
    const credential = deferred()
    vi.spyOn(operatorClient, 'tenantAgentState').mockReturnValue(tenant.promise)
    vi.spyOn(operatorClient, 'tenantOverlayState').mockResolvedValue({ revision: 3 })
    vi.spyOn(operatorClient, 'credentialState').mockReturnValue(credential.promise)
    render(<RunbooksPanel sessionEnvironment="staging" />)
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tenant-a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load tenant state' }))
    fireEvent.change(screen.getByLabelText('Credential handle'), { target: { value: 'handle-a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load credential state' }))
    fireEvent.change(screen.getByLabelText('Tenant ID'), { target: { value: 'tenant-b' } })
    fireEvent.change(screen.getByLabelText('Credential handle'), { target: { value: 'handle-b' } })
    await act(async () => {
      tenant.resolve({ agent_disabled: false, revision: 3 })
      credential.resolve({ revision: 7, rotated_at: 'Yesterday' })
    })
    expect(screen.queryByText('false')).toBeNull()
    expect(screen.queryByText('Yesterday')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Propose pause' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Propose rotate' })).toBeNull()
  })
})
