// @vitest-environment jsdom
import React from 'react'
import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ResultPanel from './ResultPanel.jsx'

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

const table = {
  columns: ['name', 'count', 'ratio'],
  rows: [['Panels', '1,204', '0.75'], ['Fasteners', 42, -0.5]],
}

function envelope(data) {
  return { ok: true, tool: 'table-demo', version: '1.0.0', timing_ms: 1, result: data }
}

function mockPointer(coarse) {
  const matchMedia = vi.fn((query) => ({ matches: coarse && query === '(pointer: coarse)' }))
  vi.stubGlobal('matchMedia', matchMedia)
  return matchMedia
}

describe('ResultPanel table parity', () => {
  it('gives grid columns and key/value labels header scopes and classifies numeric cells', () => {
    const { container } = render(<ResultPanel result={envelope({ table, count: '1,204', ratio: 0.75, name: 'Panels' })} />)
    const headers = container.querySelectorAll('thead th')
    expect(Array.from(headers, (header) => header.textContent)).toEqual(table.columns)
    headers.forEach((header) => expect(header).toHaveAttribute('scope', 'col'))
    const rows = container.querySelectorAll('.grid tbody tr')
    expect(rows).toHaveLength(table.rows.length)
    rows.forEach((row) => {
      expect(row.cells[0]).not.toHaveClass('dp-number')
      expect(row.cells[1]).toHaveClass('dp-number')
      expect(row.cells[2]).toHaveClass('dp-number')
    })
    const scalarRows = container.querySelectorAll('.kv tr')
    expect(scalarRows).toHaveLength(3)
    scalarRows.forEach((row) => {
      expect(row.cells[0].tagName).toBe('TH')
      expect(row.cells[0]).toHaveAttribute('scope', 'row')
    })
    expect(scalarRows[0].cells[1]).toHaveClass('dp-number')
    expect(scalarRows[1].cells[1]).toHaveClass('dp-number')
    expect(scalarRows[2].cells[1]).not.toHaveClass('dp-number')
  })

  it('keeps count columns semantic and numeric', () => {
    const { container } = render(<ResultPanel result={envelope({ counts: { Panels: 1204, Fasteners: '42' }, total: 1246 })} />)
    const headers = container.querySelectorAll('thead th')
    expect(Array.from(headers, (header) => header.textContent)).toEqual(['Layer', 'Count'])
    headers.forEach((header) => expect(header).toHaveAttribute('scope', 'col'))
    container.querySelectorAll('tbody tr').forEach((row) => {
      expect(row.cells[0]).not.toHaveClass('dp-number')
      expect(row.cells[1]).toHaveClass('dp-number')
    })
  })

  it.each([
    [0, true], [-12.5, true], ['-1,204.50', true], ['0', true],
    [NaN, false], [Infinity, false], [-Infinity, false], ['', false],
    [' 12', false], ['12 ', false], ['+12', false], ['.75', false],
    ['1e3', false], ['8mm', false], [null, false], [true, false],
  ])('classifies %s as numeric: %s', (value, numeric) => {
    const data = { table: { columns: ['value'], rows: [[value]] }, value }
    const { container } = render(<ResultPanel result={envelope(data)} />)
    const cells = container.querySelectorAll('tbody td')
    expect(cells).toHaveLength(2)
    cells.forEach((cell) => expect(cell.classList.contains('dp-number')).toBe(numeric))
  })

  it.each([
    ['grid and scalars', { table, count: 1204 }],
    ['counts', { counts: { Panels: 1204 } }],
    ['key/value', { count: 1204 }],
  ])('uses the prop before the pointer default for %s tables', (_name, data) => {
    const matchMedia = mockPointer(false)
    const result = envelope(data)
    const { container, rerender } = render(<ResultPanel result={result} />)
    const expectDensity = (density) => {
      const tables = container.querySelectorAll('table')
      expect(tables.length).toBeGreaterThan(0)
      tables.forEach((element) => expect(element.dataset.density).toBe(density))
    }
    expectDensity('compact')
    expect(matchMedia).toHaveBeenCalledWith('(pointer: coarse)')
    mockPointer(true)
    rerender(<ResultPanel result={result} />)
    expectDensity('comfortable')
    rerender(<ResultPanel result={result} density="compact" />)
    expectDensity('compact')
    mockPointer(false)
    rerender(<ResultPanel result={result} density="comfortable" />)
    expectDensity('comfortable')
  })

  it('defaults to compact when matchMedia is unavailable', () => {
    vi.stubGlobal('matchMedia', undefined)
    const { container } = render(<ResultPanel result={envelope({ table })} />)
    expect(container.querySelector('table').dataset.density).toBe('compact')
  })

  it('renders every row of a 10,000-row result without paging, flashing or dimming', () => {
    const rows = Array.from({ length: 10_000 }, (_, index) => [`Row ${index}`, index, '0.75'])
    const result = envelope({ table: { columns: table.columns, rows } })
    const { container } = render(<ResultPanel result={result} />)
    const renderedRows = container.querySelectorAll('.grid tbody tr')
    expect(renderedRows).toHaveLength(rows.length)
    expect(Array.from(renderedRows, (row) => row.cells[0].textContent)).toEqual(rows.map((row) => row[0]))
    expect(container.querySelector('.flash')).toBeNull()
    renderedRows.forEach((row) => {
      expect(row).not.toHaveAttribute('hidden')
      expect(row.style.display).not.toBe('none')
      expect(row.style.visibility).not.toBe('hidden')
      expect(row.style.opacity === '' || Number(row.style.opacity) >= 1).toBe(true)
    })
  }, 15_000)
})
