/**
 * OperatorEntry — acceptance #6: invisible + exactly one probe request when
 * the operator surface is unmounted; visible + openable when it is.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import OperatorEntry from './OperatorEntry.jsx'
import * as operatorClient from '../../operatorClient.js'

afterEach(cleanup)

beforeEach(() => {
  operatorClient.resetOperatorProbeForTests()
  vi.spyOn(operatorClient, 'createSession').mockResolvedValue(
    { session_id: 'opsess-1', profile: 'default', environment: 'staging', status: 'idle' })
  vi.spyOn(operatorClient, 'listSessions').mockResolvedValue([])
  vi.spyOn(operatorClient, 'getAudit').mockResolvedValue([])
})

describe('acceptance #6: LEAF_OPERATOR_ENABLED unset -> hidden, one probe, then none', () => {
  it('renders nothing when the probe reports disabled', async () => {
    vi.spyOn(operatorClient, 'probeOperatorConsole').mockResolvedValue(false)
    const { container } = render(<OperatorEntry />)
    await waitFor(() => expect(operatorClient.probeOperatorConsole).toHaveBeenCalled())
    expect(container.firstChild).toBeNull()
  })

  it('probes exactly once even across a remount (StrictMode-style double effect)', async () => {
    vi.spyOn(operatorClient, 'probeOperatorConsole')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ status: 404, ok: false, json: async () => ({}) }))
    const first = render(<OperatorEntry />)
    first.unmount()
    render(<OperatorEntry />)
    await waitFor(() => expect(operatorClient.probeOperatorConsole).toHaveBeenCalledTimes(2))
    // probeOperatorConsole is called per-mount, but its OWN singleton cache
    // (proven in operatorClient.test.js) means only one fetch ever fires.
    expect(fetch).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })
})

describe('when the operator surface is mounted', () => {
  it('shows an accessible "Open operator console" control and opens the drawer', async () => {
    vi.spyOn(operatorClient, 'probeOperatorConsole').mockResolvedValue(true)
    render(<OperatorEntry />)
    const openButton = await screen.findByRole('button', { name: /open operator console/i })
    fireEvent.click(openButton)
    expect(await screen.findByRole('dialog', { name: /operator console/i })).toBeTruthy()
  })

  it('closing the console returns focus to the rail button that opened it', async () => {
    vi.spyOn(operatorClient, 'probeOperatorConsole').mockResolvedValue(true)
    render(<OperatorEntry />)
    const openButton = await screen.findByRole('button', { name: /open operator console/i })
    fireEvent.click(openButton)
    const dialog = await screen.findByRole('dialog')
    dialog.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await waitFor(() => expect(document.activeElement).toBe(openButton))
  })
})
