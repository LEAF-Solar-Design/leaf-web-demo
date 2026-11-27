import { Blob as NodeBlob } from 'node:buffer'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import ResultPanel from './ResultPanel.jsx'

const resultWithRows = (layers) => ({
  ok: true,
  tool: 'count-modules',
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

it('exports assignment capacities and keeps scalar string references on the JSON path', async () => {
  const result = {
    ok: true,
    tool: 'solar-assignment-report',
    result: {
      saved_state: {
        collector_capacity: [{ inverter_ref: 'INV-1', total_dc_inputs: 4, occupied_inputs: 3, spare_inputs: 1 }],
        unassigned_string_refs: ['STR-4', 'STR-5'],
      },
    },
  }
  render(<ResultPanel result={result} />)
  const csv = screen.getByRole('link', { name: /^Download Saved state \/ Collector capacity CSV$/i })
  expect(await readDownload(csv)).toContain('"INV-1","4","3","1"')
  expect(screen.getAllByRole('link', { name: /CSV$/ })).toEqual([csv])
  expect(screen.queryByRole('button', { name: 'Jump to layer' })).toBeNull()
  expect(JSON.parse(await readDownload(jsonLink()))).toEqual(result)
})

it('exports preset list rows without attaching a layer jump to a preset name', async () => {
  render(<ResultPanel result={{
    ok: true,
    tool: 'solar-design-presets-list',
    result: { list_rows: [{ prefix: 'A', name: 'East roof' }] },
  }} />)
  const csv = screen.getByRole('link', { name: /^Download List rows CSV$/i })
  expect(await readDownload(csv)).toBe('"prefix","name"\r\n"A","East roof"\r\n')
  expect(screen.queryByRole('button', { name: 'Jump to layer' })).toBeNull()
})

it('omits CSV when a table is beneath a redacted result field', async () => {
  render(<ResultPanel result={{
    ok: true,
    tool: 'solar-design-presets-list',
    result: { secret: { rows: [{ name: 'placeholder' }] } },
  }} />)
  expect(screen.queryByRole('link', { name: /CSV$/ })).toBeNull()
  expect(JSON.parse(await readDownload(jsonLink())).result.secret).toBe('[redacted]')
})

const redactedLink = () => screen.getByRole('link', { name: 'Download Pooled shares with layer names redacted CSV' })
const pooledResult = (report, tool = 'count-modules') => ({
  ok: true,
  tool,
  result: { pooled_cumulative_shares: report },
})

it.each(['count-modules', 'another-comparison-tool'])('exports an explicit total and no layer metadata for %s', async (tool) => {
  const layers = [
    { layer: 'Private Roof', note: 'Private note', increment_numerator: 3, numerator: 3, denominator: 4, percentage: 75 },
    { layer: 'Private Annex', increment_numerator: 1, numerator: 4, denominator: 4, percentage: 100 },
  ]
  render(<ResultPanel result={pooledResult({ defined: true, layers }, tool)} />)
  expect(await readDownload(redactedLink())).toBe([
    '"rank","increment_numerator","numerator","denominator","percentage"',
    '"1","3","3","4","75"',
    '"2","1","4","4","100"',
    '"Total","4","4","4","100"',
    '',
  ].join('\r\n'))
  expect(JSON.parse(await readDownload(jsonLink())).result.pooled_cumulative_shares.layers).toEqual(layers)
})

it.each([Infinity, NaN, '100', undefined])('clears the redacted export for an invalid measure (%s)', (percentage) => {
  const layer = { layer: 'Roof', increment_numerator: 1, numerator: 1, denominator: 1, percentage: 100 }
  const view = render(<ResultPanel result={pooledResult({ defined: true, layers: [layer] })} />)
  const prior = redactedLink().getAttribute('href')
  const name = 'Download Pooled shares with layer names redacted CSV'
  view.rerender(<ResultPanel result={pooledResult({ defined: true, layers: [{ ...layer, percentage }] })} />)
  expect(screen.queryByRole('link', { name })).toBeNull()
  expect(screen.getByText('Pooled shares with layer names redacted CSV download is unavailable.'))
    .toHaveAttribute('role', 'status')
  expect(URL.revokeObjectURL).toHaveBeenCalledWith(prior)

  view.rerender(<ResultPanel result={pooledResult({ defined: false, layers: [] })} />)
  expect(screen.queryByRole('link', { name })).toBeNull()
  expect(screen.queryByText(/redacted CSV download is unavailable/)).toBeNull()
})
