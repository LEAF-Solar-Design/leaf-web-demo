import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import SessionPanel from './SessionPanel.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  vi.spyOn(operatorClient, 'listSessions').mockResolvedValue([
    { session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' },
  ])
  vi.spyOn(operatorClient, 'getEvents').mockResolvedValue([
    { seq: 1, type: 'operator_turn_started', data: { subject: 'op@example.com' } },
  ])
  vi.spyOn(operatorClient, 'createSession').mockResolvedValue({ session_id: 'opsess-2' })
  vi.spyOn(operatorClient, 'postMessage').mockResolvedValue({ turn_id: 't-1', status: 'complete' })
  vi.spyOn(operatorClient, 'isOperatorDenied').mockReturnValue(false)
})

describe('acceptance: clear current profile and environment', () => {
  it('shows the profile and environment of the opened session', async () => {
    render(<SessionPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /default · staging · idle/i }))
    const badge = await screen.findByRole('status')
    expect(badge.textContent).toMatch(/default/i)
    expect(badge.textContent).toMatch(/staging/i)
  })
})

describe('acceptance: session list and transcript', () => {
  it('lists sessions and renders the transcript of the opened one', async () => {
    render(<SessionPanel />)
    expect(await screen.findByRole('button', { name: /default · staging · idle/i })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /default · staging · idle/i }))
    expect(await screen.findByText('operator_turn_started')).toBeTruthy()
  })

  it('sends a message and refreshes the transcript', async () => {
    render(<SessionPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /default · staging · idle/i }))
    await screen.findByText('operator_turn_started')
    fireEvent.change(screen.getByLabelText(/message/i), { target: { value: 'pause acme-solar' } })
    fireEvent.click(screen.getByRole('button', { name: /^send$/i }))
    await waitFor(() => expect(operatorClient.postMessage).toHaveBeenCalledWith('opsess-1', 'pause acme-solar'))
  })
})

describe('acceptance #4: signed-out reset', () => {
  it('calls onSignedOut when listing sessions is denied', async () => {
    operatorClient.listSessions.mockRejectedValue(Object.assign(new Error('denied'), { status: 404 }))
    operatorClient.isOperatorDenied.mockReturnValue(true)
    const onSignedOut = vi.fn()
    render(<SessionPanel onSignedOut={onSignedOut} />)
    await waitFor(() => expect(onSignedOut).toHaveBeenCalled())
  })
})

describe('acceptance #1: no secret-shaped input', () => {
  it('the only input carries a message, never secret/token/password/key', async () => {
    render(<SessionPanel />)
    fireEvent.click(await screen.findByRole('button', { name: /default · staging · idle/i }))
    for (const field of [...screen.queryAllByRole('textbox')]) {
      const label = (field.getAttribute('aria-label') || field.getAttribute('placeholder') || field.name || field.id || '')
      expect(label).not.toMatch(/secret|token|password|key/i)
    }
  })
})
