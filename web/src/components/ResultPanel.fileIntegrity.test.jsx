import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import ResultPanel from './ResultPanel.jsx'

const envelope = file => ({ ok: true, result: { layers: [{ name: 'Preserved roof', area: 20 }], files: [file] } })
const file = { name: 'report.json', mime: 'application/json', base64: btoa('{}') }
beforeEach(() => vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn(() => 'blob:report'), revokeObjectURL: vi.fn() }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('C02 optional report file byte count', () => {
  it('rejects syntactically valid but truncated bytes and gives a recovery action without losing rows', () => {
    render(<ResultPanel result={envelope({ ...file, bytes: 40, base64: btoa('{') })} />)
    expect(screen.getByRole('cell', { name: 'Preserved roof' })).toBeVisible()
    expect(screen.queryByText('Download report.json')).toBeNull()
    expect(screen.getByText('report.json (unreadable)')).toBeVisible()
    expect(screen.getByText(/Report file size does not match.*Run the report again/)).toBeVisible()
  })

  it.each([-1, 1.5, '2', true, null])('rejects invalid declared byte count %s', bytes => {
    render(<ResultPanel result={envelope({ ...file, bytes })} />)
    expect(screen.queryByText('Download report.json')).toBeNull()
    expect(screen.getByText(/Report file size is invalid.*Run the report again/)).toBeVisible()
  })

  it.each([file, { ...file, bytes: 2 }, { ...file, bytes: 0, base64: '' }, { ...file, bytes: 3, base64: btoa(String.fromCharCode(0xe5, 0xb1, 0x8b)) }])(
    'preserves valid legacy and exact binary byte-count downloads', supplied => {
      render(<ResultPanel result={envelope(supplied)} />)
      expect(screen.getByText('Download report.json')).toBeVisible()
    })
})
