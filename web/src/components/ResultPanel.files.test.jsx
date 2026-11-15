import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import ResultPanel from './ResultPanel.jsx'

const file = (name = 'report.json', base64 = btoa('{"layers":[]}')) => ({ name, base64, mime: 'application/json' })
const result = files => ({ ok: true, tool: 'layer-bounding-boxes', result: { files } })
let create
let revoke
beforeEach(() => {
  let sequence = 0
  create = vi.fn(() => `blob:report-${++sequence}`)
  revoke = vi.fn()
  vi.stubGlobal('URL', { ...URL, createObjectURL: create, revokeObjectURL: revoke })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('C02 report file URL ownership', () => {
  it('does not allocate again for an unrelated panel rerender and revokes on unmount', () => {
    const envelope = result([file()])
    const { rerender, unmount } = render(<ResultPanel result={envelope} running={false} />)
    expect(screen.getByText('Download report.json')).toHaveAttribute('href', 'blob:report-1')
    rerender(<ResultPanel result={envelope} running={false} density="comfortable" />)
    expect(create).toHaveBeenCalledTimes(1)
    expect(revoke).not.toHaveBeenCalled()
    unmount()
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:report-1')
  })

  it('retires the old report URL when files are replaced or removed', () => {
    const { rerender } = render(<ResultPanel result={result([file()])} />)
    rerender(<ResultPanel result={result([file('second.json')])} />)
    expect(screen.queryByText('Download report.json')).toBeNull()
    expect(screen.getByText('Download second.json')).toHaveAttribute('href', 'blob:report-2')
    expect(revoke).toHaveBeenCalledExactlyOnceWith('blob:report-1')
    rerender(<ResultPanel result={result([])} />)
    expect(screen.queryByText('Download second.json')).toBeNull()
    expect(revoke.mock.calls).toEqual([['blob:report-1'], ['blob:report-2']])
  })

  it('shows malformed bytes as unreadable without allocating or revoking a URL', () => {
    const { unmount } = render(<ResultPanel result={result([file('invalid.json', '%%%')])} />)
    expect(screen.getByText('invalid.json (unreadable)')).toBeInTheDocument()
    expect(create).not.toHaveBeenCalled()
    unmount()
    expect(revoke).not.toHaveBeenCalled()
  })
})
