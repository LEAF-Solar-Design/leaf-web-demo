// @vitest-environment jsdom
import React from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DiagnosticsDetails from './DiagnosticsDetails.jsx'
import { createDiagnostics } from './diagnostics.js'

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else delete navigator.clipboard
})

function stubClipboard(clipboard) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: clipboard })
}

function openDetails(container) {
  const details = container.querySelector('details')
  details.open = true
  fireEvent(details, new Event('toggle'))
  return details
}

describe('connection details', () => {
  it('renders a read-only view and refreshes the snapshot each time it opens', () => {
    const diagnostics = createDiagnostics({ now: () => 0 })
    diagnostics.record('start', { kind: 'none' })
    const { container } = render(<DiagnosticsDetails diagnostics={diagnostics} />)
    expect(screen.getByText('Connection details').tagName).toBe('SUMMARY')
    const details = openDetails(container)
    const textarea = screen.getByRole('textbox', { name: 'Connection details' })
    expect(textarea.readOnly).toBe(true)
    expect(textarea.value).toBe(diagnostics.snapshot())
    details.open = false
    fireEvent(details, new Event('toggle'))
    diagnostics.record('handshake', { kind: 'ready' })
    openDetails(container)
    expect(textarea.value).toBe(diagnostics.snapshot())
    expect(textarea.value).toContain('handshake  kind=ready')
  })

  it('copies the displayed snapshot and announces success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    stubClipboard({ writeText })
    const diagnostics = createDiagnostics()
    const { container } = render(<DiagnosticsDetails diagnostics={diagnostics} />)
    openDetails(container)
    fireEvent.click(screen.getByRole('button', { name: 'Copy connection details' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Copied.'))
    expect(writeText).toHaveBeenCalledWith(screen.getByRole('textbox').value)
  })

  it.each(['missing', 'rejected'])('selects the text when clipboard access is %s', async (mode) => {
    stubClipboard(mode === 'missing' ? undefined : {
      writeText: vi.fn().mockRejectedValue(new Error('Denied')),
    })
    const { container } = render(<DiagnosticsDetails diagnostics={createDiagnostics()} />)
    openDetails(container)
    const textarea = screen.getByRole('textbox')
    fireEvent.click(screen.getByRole('button', { name: 'Copy connection details' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('Select the text above and copy it.'))
    expect(document.activeElement).toBe(textarea)
    expect(textarea.selectionStart).toBe(0)
    expect(textarea.selectionEnd).toBe(textarea.value.length)
  })
})
