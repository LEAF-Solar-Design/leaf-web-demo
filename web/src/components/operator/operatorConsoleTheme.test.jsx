import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

import OperatorConsole from './OperatorConsole.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

beforeEach(() => {
  vi.spyOn(operatorClient, 'createSession').mockResolvedValue(
    { session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' })
  vi.spyOn(operatorClient, 'listSessions').mockResolvedValue([])
  vi.spyOn(operatorClient, 'getAudit').mockResolvedValue([])
  vi.spyOn(operatorClient, 'isOperatorDenied').mockImplementation((e) => [401, 403, 404].includes(e?.status))
})

it('sets the Stage theme on the operator console dialog', async () => {
  render(<OperatorConsole onClose={vi.fn()} />)
  await screen.findByRole('status')
  expect(screen.getByRole('dialog', { name: 'Operator console' }).getAttribute('data-leaf-theme')).toBe('stage')
})
