import React from 'react'
import { Blob as NodeBlob } from 'node:buffer'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ResultJsonDownload from './ResultJsonDownload.jsx'
import ResultPanel from './ResultPanel.jsx'

const report = () => ({
  ok: true, tool: 'inspect-extents', version: '2.3.4', timing_ms: 17,
  project_context: { project_id: 'synthetic-project', input_version_id: 'synthetic-version', intake_sha256: 'a'.repeat(64) },
  result: { rows: [
    { layer: 'Large', area: 123.123456789, min_x: -0.000000123456 },
    { layer: 'Small', area: 7.987654321, min_x: 0.123456789 },
  ] },
})

let createObjectURL
let revokeObjectURL
beforeEach(() => {
  createObjectURL = vi.fn().mockReturnValueOnce('blob:first').mockReturnValue('blob:next')
  revokeObjectURL = vi.fn()
  vi.stubGlobal('Blob', NodeBlob)
  vi.stubGlobal('URL', { createObjectURL, revokeObjectURL })
})
afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

async function exported(index = 0) {
  const blob = createObjectURL.mock.calls[index][0]
  expect(blob.type).toBe('application/json')
  return JSON.parse(await blob.text())
}

describe('generic result JSON download', () => {
  it('exports the actual row report with precision, order and returned identities through ResultPanel', async () => {
    const result = report()
    const before = JSON.stringify(result)
    render(<ResultPanel running={false} result={result} />)
    const link = screen.getByRole('link', { name: 'Download result JSON' })
    expect(link).toHaveAttribute('href', 'blob:first')
    expect(link).toHaveAttribute('download', 'result.json')
    expect(await exported()).toEqual(result)
    expect(JSON.stringify(result)).toBe(before)
  })

  it('exports every returned row even when the panel paginates them', async () => {
    const result = report()
    result.result.rows = Array.from({ length: 107 }, (_, i) => ({ layer: `Layer ${i}`, area: i + 0.123456789 }))
    const view = render(<ResultPanel running={false} result={result} />)
    expect(view.container.querySelectorAll('table.counts tbody tr')).toHaveLength(100)
    expect((await exported()).result.rows).toEqual(result.result.rows)
  })

  it('replaces the file on a filtered rerun and releases URLs on replacement and scope clear', async () => {
    const first = report()
    const view = render(<ResultPanel running={false} result={first} />)
    const next = { ...first, result: { rows: [first.result.rows[1]] } }
    view.rerender(<ResultPanel running={false} result={next} />)
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:first')
    expect(screen.getByRole('link', { name: 'Download result JSON' })).toHaveAttribute('href', 'blob:next')
    expect(await exported(1)).toEqual(next)
    view.rerender(<ResultPanel running={false} result={null} />)
    expect(screen.queryByRole('link', { name: 'Download result JSON' })).toBeNull()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:next')
  })

  it('uses the shared redaction policy for credential-shaped fields without changing drawing values', async () => {
    const result = report()
    result.result.rows[0].token = 'synthetic-sensitive-value'
    result.project_context['authorization'] = ['Bearer', 'synthetic-value'].join(' ')
    render(<ResultJsonDownload result={result} />)
    const copy = await exported()
    expect(copy.result.rows[0].token).toBe('[redacted]')
    expect(copy.project_context.authorization).toBe('[redacted]')
    expect(copy.result.rows[0].area).toBe(result.result.rows[0].area)
    expect(result.result.rows[0].token).toBe('synthetic-sensitive-value')
  })

  it('offers no file for failures or output that cannot be serialized', () => {
    const view = render(<ResultJsonDownload result={{ ...report(), ok: false }} />)
    expect(screen.queryByRole('link')).toBeNull()
    expect(createObjectURL).not.toHaveBeenCalled()
    const cycle = report()
    cycle.result.self = cycle.result
    view.rerender(<ResultJsonDownload result={cycle} />)
    expect(screen.getByRole('status')).toHaveTextContent('Result download is unavailable.')
    expect(screen.queryByRole('link')).toBeNull()
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('releases the current file when the panel unmounts', () => {
    const view = render(<ResultJsonDownload result={report()} />)
    view.unmount()
    expect(revokeObjectURL).toHaveBeenCalledExactlyOnceWith('blob:first')
  })
})
