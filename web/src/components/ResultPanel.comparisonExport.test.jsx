import { Blob as NodeBlob } from 'node:buffer'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ResultPanel from './ResultPanel.jsx'

const resultWithRows = (layers, tool = 'count-modules') => ({
  ok: true,
  tool,
  result: { layer_rank_change: { layers }, warnings: ['Partial snapshot'] },
})
const csvLink = () => screen.getByRole('link', { name: /Download Layer rank change \/ Layers CSV/i })
const jsonLink = () => screen.getByRole('link', { name: 'Download result JSON' })
const blobs = new Map()
const readDownload = (link) => blobs.get(link.getAttribute('href')).text()

beforeEach(() => {
  vi.stubGlobal('Blob', NodeBlob)
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn((blob) => {
      const url = `blob:comparison-${blobs.size}`
      blobs.set(url, blob)
      return url
    }),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  blobs.clear()
})

it('exports CSV and JSON from the same result and replaces the old download when rows change', async () => {
  const first = resultWithRows([{ layer: 'Café, "East"\nRoof', movement: -1 }])
  const second = resultWithRows([
    { layer: '=1+1', movement: 2 },
    { layer: 'Cafe\u0301', details: { before: 2 } },
  ])
  const view = render(<ResultPanel result={first} />)
  expect(await readDownload(csvLink())).toBe('"layer","movement"\r\n"Café, ""East""\nRoof","-1"\r\n')
  expect(JSON.parse(await readDownload(jsonLink()))).toEqual(first)
  const oldCsv = csvLink().getAttribute('href')

  view.rerender(<ResultPanel result={second} />)
  expect(await readDownload(csvLink())).toContain(`"'=1+1","2",\r\n"Cafe\u0301",,"{""before"":2}"`)
  expect(JSON.parse(await readDownload(jsonLink()))).toEqual(second)
  expect(URL.revokeObjectURL).toHaveBeenCalledWith(oldCsv)
})

it('downloads all comparison rows independently of the visible page', async () => {
  const rows = Array.from({ length: 101 }, (_, index) => ({ layer: `Layer ${index}`, movement: index }))
  render(<ResultPanel result={resultWithRows(rows)} />)
  const firstDownload = await readDownload(csvLink())
  expect(firstDownload.split('\r\n')).toHaveLength(103)
  expect(firstDownload).toContain('"Layer 100","100"')
  fireEvent.click(screen.getByRole('button', { name: /Next Layer rank change \/ Layers page/i }))
  expect(await readDownload(csvLink())).toBe(firstDownload)
})

it('jumps across pages to the exact Unicode layer and leaves its surrounding cells visible', () => {
  const rows = Array.from({ length: 102 }, (_, index) => ({ layer: `Layer ${index}`, movement: index }))
  rows[0].layer = 'Café'
  rows[100].layer = 'Cafe\u0301'
  render(<ResultPanel result={resultWithRows(rows)} />)
  const input = screen.getByRole('textbox', { name: 'Layer name' })
  fireEvent.change(input, { target: { value: 'Cafe\u0301' } })
  fireEvent.click(screen.getByRole('button', { name: 'Jump to layer' }))
  expect(screen.getByRole('row', { name: 'Cafe\u0301 100' })).toHaveFocus()
  expect(screen.getByRole('row', { name: 'Layer 101 101' })).toBeVisible()

  fireEvent.change(input, { target: { value: 'missing' } })
  fireEvent.keyDown(input, { key: 'Enter' })
  expect(screen.getByText('No layer matches that name.')).toBeVisible()
  expect(screen.getByRole('row', { name: 'Layer 101 101' })).toBeVisible()
})

it('keeps scalar lists and results from other tools on their existing export paths', () => {
  const rows = [{ layer: 'East roof', movement: 1 }]
  const view = render(<ResultPanel result={resultWithRows(rows, 'another-tool')} />)
  expect(screen.queryByRole('link', { name: /CSV$/ })).toBeNull()
  expect(jsonLink()).toBeVisible()

  const withScalarList = resultWithRows(rows)
  withScalarList.result.notes = ['Counted', 'Compared']
  view.rerender(<ResultPanel result={withScalarList} />)
  expect(screen.getAllByRole('link', { name: /CSV$/ })).toEqual([csvLink()])
})
