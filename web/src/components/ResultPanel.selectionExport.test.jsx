import React from 'react'
import { Blob as NodeBlob } from 'node:buffer'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ResultPanel from './ResultPanel.jsx'

const selection = (version, handles) => ({
  ok: true,
  tool: 'x05-selection',
  version,
  timing_ms: 1,
  overlay: { highlight_handles: handles },
  result: { count: handles.length },
})

const linkName = 'Download selection JSON'
const exportsByUrl = new Map()

beforeEach(() => {
  vi.stubGlobal('Blob', NodeBlob)
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn((blob) => {
      const url = `blob:selection-${exportsByUrl.size}`
      exportsByUrl.set(url, blob)
      return url
    }),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  exportsByUrl.clear()
})

async function readDownload(link) {
  const blob = exportsByUrl.get(link.getAttribute('href'))
  expect(blob.type).toBe('application/json')
  return JSON.parse(await blob.text())
}

it('exports the full, narrowed, and restored handle selections in their returned order', async () => {
  // The local catalog revision/rollback replay returned these versions and handle arrays.
  const before = selection('1.0.0', ['B', 'A'])
  const revised = selection('1.0.1', ['A'])
  const restored = selection('1.0.0', ['B', 'A'])
  const view = render(<ResultPanel result={before} />)

  let previousUrl
  for (const result of [before, revised, restored]) {
    view.rerender(<ResultPanel result={result} />)
    const link = screen.getByRole('link', { name: linkName })
    expect(link).toHaveAttribute('download', 'selection.json')
    const expected = { tool: result.tool, version: result.version, handles: result.overlay.highlight_handles }
    expect(await readDownload(link)).toEqual(expected)
    if (previousUrl) expect(URL.revokeObjectURL).toHaveBeenCalledWith(previousUrl)
    previousUrl = link.getAttribute('href')
  }

  view.rerender(<ResultPanel running result={restored} />)
  expect(screen.queryByRole('link', { name: linkName })).toBeNull()
  expect(URL.revokeObjectURL).toHaveBeenCalledWith(previousUrl)
})

it('exports an explicit empty selection even when the tool has no result body', async () => {
  render(<ResultPanel result={{ ...selection('1.0.0', []), result: null }} />)
  expect(await readDownload(screen.getByRole('link', { name: linkName }))).toEqual({
    tool: 'x05-selection', version: '1.0.0', handles: [],
  })
})

it('keeps the selection file limited to its handles and tool version when a row report is also available', async () => {
  const result = {
    ...selection('1.0.1', ['A']),
    result: { rows: [{ layer: 'East roof', count: 1 }] },
  }
  render(<ResultPanel result={result} />)
  expect(await readDownload(screen.getByRole('link', { name: 'Download result JSON' }))).toEqual(result)
  expect(await readDownload(screen.getByRole('link', { name: linkName }))).toEqual({
    tool: 'x05-selection', version: '1.0.1', handles: ['A'],
  })
})

it.each([
  { ok: false },
  { overlay: { markers: [] } },
  { overlay: { highlight_handles: ['A', 12] } },
])('offers no selection file for %j', (change) => {
  render(<ResultPanel result={{ ...selection('1.0.0', ['A']), ...change }} />)
  expect(screen.queryByRole('link', { name: linkName })).toBeNull()
  expect(URL.createObjectURL).not.toHaveBeenCalled()
})
