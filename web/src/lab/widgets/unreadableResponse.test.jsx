import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { loadWidget, saveWidget } from './widgetsClient.js'
import { createWidgetDoc } from './useWidgetEditor.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = body => ({ ok: true, status: 200, json: async () => body })

const unreadable = [
  ['null', async () => null],
  ['array', async () => []],
  ['rejected JSON', async () => { throw new Error('synthetic non-JSON response') }],
]

for (const operation of ['load', 'save']) {
  it.each(unreadable)(`${operation} rejects HTTP200 %s without retry`, async (_, json) => {
    const fetch = vi.fn(async () => ({ ok: true, status: 200, json }))
    const result = operation === 'load' ? await loadWidget('unreadable-proof', { fetch })
      : await saveWidget('unreadable-proof', createWidgetDoc(), 1, { fetch })
    expect(result).toEqual({ ok: false, status: 502, detail: 'The document store returned an unreadable response.' })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(result).not.toHaveProperty('doc')
    expect(result).not.toHaveProperty('revision')
  })
}

for (const operation of ['load', 'save']) {
  it.each(unreadable)(`${operation} keeps rendered draft/head after %s and recovers only on explicit Save`, async (label, json) => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ revision: 1 }))
      .mockResolvedValueOnce({ ok: true, status: 200, json })
      .mockResolvedValueOnce(response({ revision: 2 }))
    vi.stubGlobal('fetch', fetch)
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: `invalid-${operation}-${label.replaceAll(' ', '-')}` } })
    fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true })))
    expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1.')
    fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Retain malformed-response draft' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: operation === 'load' ? 'Load' : 'Save', exact: true })))
    expect(screen.getByRole('alert')).toHaveTextContent('The document store returned an unreadable response.')
    expect(screen.getByRole('status')).toHaveTextContent('Your local edits are kept.')
    expect(screen.getByLabelText('Text')).toHaveValue('Retain malformed-response draft')
    expect(fetch).toHaveBeenCalledTimes(2)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true })))
    expect(JSON.parse(fetch.mock.calls[2][1].body).expected_head).toBe(1)
    expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2.')
    expect(screen.getByLabelText('Text')).toHaveValue('Retain malformed-response draft')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(fetch).toHaveBeenCalledTimes(3)
  })
}
