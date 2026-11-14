import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import WidgetsView from './WidgetsView.jsx'
import { saveWidget } from './widgetsClient.js'
import { createWidgetDoc } from './useWidgetEditor.js'

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })
const response = body => ({ ok: true, status: 200, json: async () => body })

describe('widget save revision receipts', () => {
  it.each([undefined, null, 0, -1, true, '1', 1.5, Number.MAX_SAFE_INTEGER + 1])(
    'rejects an HTTP 200 save with invalid revision %s', async revision => {
      const fetch = vi.fn(async () => response({ revision }))
      expect(await saveWidget('summary', createWidgetDoc(), null, { fetch })).toMatchObject({ ok: false, status: 502 })
      expect(fetch).toHaveBeenCalledTimes(1)
    },
  )

  it('preserves a valid revision receipt and the exact save payload', async () => {
    const fetch = vi.fn(async () => response({ revision: 3, sha256: 'receipt-digest' }))
    const doc = createWidgetDoc()
    expect(await saveWidget('summary', doc, 2, { fetch })).toMatchObject({ ok: true, revision: 3, sha256: 'receipt-digest' })
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ graph: doc, expected_head: 2, kind: 'widget' })
  })

  it('retains unsaved edits and the previous head after a missing save receipt', async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response({ revision: 1 }))
      .mockResolvedValueOnce(response({}))
      .mockResolvedValueOnce(response({ revision: 2 }))
    vi.stubGlobal('fetch', fetch)
    render(<WidgetsView />)
    fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'summary' } })
    fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 1'))
    fireEvent.change(screen.getByLabelText('Text'), { target: { value: 'Still unsaved' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    await screen.findByRole('alert')
    expect(screen.getByRole('status')).toHaveTextContent('request failed')
    expect(screen.getByLabelText('Text')).toHaveValue('Still unsaved')
    expect(screen.queryByRole('button', { name: 'Request publication' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save', exact: true }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Saved revision 2'))
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toMatchObject({ expected_head: 1 })
  })
})
