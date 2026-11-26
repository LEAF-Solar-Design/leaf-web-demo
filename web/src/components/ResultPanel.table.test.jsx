import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ResultPanel from './ResultPanel.jsx'


afterEach(cleanup)

const envelope = {
  ok: true,
  tool: 'table-export-demo',
  version: '1.0.0',
  timing_ms: 212,
  cost: null,
  error: null,
  result: {
    row_count: 2,
    table: {
      columns: ['Material/Dim', 'Length (mm)', 'Quantity', 'Notes'],
      rows: [
        ['PANEL_A', '3000', 8, ''],
        ['FASTENER_B', '250', 20, '8mm diameter'],
      ],
    },
    warnings: ['One source row was omitted.'],
    files: [
      { name: 'result.csv', mime: 'text/csv', bytes: 5, base64: btoa('a,b,c') },
    ],
  },
}

describe('ResultPanel generic table results', () => {
  it('hides geometry and structured cells and keeps short ids on one line', () => {
    const jump = vi.fn()
    render(<ResultPanel navigation={{ jump }} result={{ ok: true, result: { table: {
      columns: ['id', 'Modules', 'pts', 'geometry', 'metadata'],
      rows: [['S001', 17, [[1, 2], [3, 4]], { type: 'Polygon' }, ['hidden-value']]],
    } } }} />)
    expect(screen.queryByRole('columnheader', { name: 'pts' })).toBeNull()
    expect(screen.queryByRole('columnheader', { name: 'geometry' })).toBeNull()
    expect(screen.queryByText('hidden-value')).toBeNull()
    expect(screen.queryByText('[object Object]')).toBeNull()
    expect(screen.getByRole('cell', { name: 'S001' })).toHaveStyle({ whiteSpace: 'nowrap', minWidth: '64px' })
    expect(screen.getByRole('cell', { name: '17' })).toHaveStyle({ textAlign: 'right', fontVariantNumeric: 'tabular-nums' })
    fireEvent.click(screen.getByRole('button', { name: 'View S001 on canvas' }))
    expect(jump).toHaveBeenCalledWith('S001')
  })
  it('renders result.table rows, warnings, scalars and file downloads', () => {
    const createObjectURL = vi.fn(() => 'blob:result')
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() })
    render(<ResultPanel running={false} tool={{ name: 'table-export-demo' }} result={envelope} />)
    expect(screen.getByText('Material/Dim')).toBeInTheDocument()
    expect(screen.getByText('PANEL_A')).toBeInTheDocument()
    expect(screen.getByText('8mm diameter')).toBeInTheDocument()
    expect(screen.getByText('One source row was omitted.')).toBeInTheDocument()
    const link = screen.getByText('Download result.csv')
    expect(link.getAttribute('href')).toBe('blob:result')
    expect(link.getAttribute('download')).toBe('result.csv')
    expect(createObjectURL).toHaveBeenCalledTimes(1)
    vi.unstubAllGlobals()
  })

  it('leaves the counts path untouched', () => {
    render(<ResultPanel running={false} tool={{ name: 'count-by-layer' }}
      result={{ ok: true, tool: 'count-by-layer', version: '1.0.0', timing_ms: 1, cost: null, error: null,
        result: { counts: { Panels: 3 }, total: 3 } }} />)
    expect(screen.getByText('Panels')).toBeInTheDocument()
  })
})

describe('ResultPanel scalar lists', () => {
  it.each([
    ['named layer list', { layers: ['ROOF', 'PV-ROW', 'ROOF'] }, 'layers', ['ROOF', 'PV-ROW', 'ROOF']],
    ['nested mixed values', { details: { values: [0, false, null, '', 3.5] } }, 'details / values', ['0', 'false', 'null', '""', '3.5']],
    ['root list', [false, 12, true], 'result', ['false', '12', 'true']],
    ['generic strings field', { strings: ['one', 'two'] }, 'strings', ['one', 'two']],
  ])('renders %s in order', (_case, data, label, expected) => {
    render(<ResultPanel result={{ ok: true, result: data }} />)
    const table = screen.getByRole('table', { name: label })
    expect(within(table).getAllByRole('cell').map(cell => cell.textContent)).toEqual(expected)
    expect(screen.getAllByRole('cell')).toHaveLength(expected.length)
  })

  it('paginates long lists beside structured rows without repeating their cells', () => {
    const values = Array.from({ length: 101 }, (_, index) => `Layer ${index + 1}`)
    render(<ResultPanel result={{ ok: true, result: { values, rows: [{ tags: ['roof', 'south'] }] } }} />)
    const list = screen.getByRole('table', { name: 'values' })
    expect(within(list).getAllByRole('cell')).toHaveLength(100)
    expect(within(list).queryByText('Layer 101')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Next values page' }))
    expect(within(list).getAllByRole('cell').map(cell => cell.textContent)).toEqual(['Layer 101'])
    expect(within(screen.getByRole('table', { name: 'rows' })).getByText('["roof","south"]')).toBeVisible()
    expect(screen.getAllByRole('table')).toHaveLength(2)
  })
})

describe('String result presentation', () => {
  const stringResult = { ok: true, tool: 'string-panels', result: {
    strings: [{ id: 'S001', modules: 2, pts: [[0, 0], [1, 0]], voc_at_tmin: 100, inverter: 'INV1' },
      { id: 'S002', modules: 1, pts: [[2, 0]], voc_at_tmin: 50, inverter: 'INV1' }],
    stats: { string_count: 2, modules_stringed: 3 },
    electrical: { worst_string_voc: 100, pass: true }, module: { max_system_voltage: 600 },
  } }
  it('renders the four requested columns, bold total and reachable locate control', () => {
    const jump = vi.fn()
    const { container } = render(<ResultPanel result={stringResult} navigation={{ jump }} />)
    expect(screen.getAllByRole('columnheader').map(e => e.textContent)).toEqual(['String', 'Modules', 'Voc at Tmin', 'Inverter', 'Locate'])
    expect(screen.getByText('Total').closest('tfoot')).toHaveStyle({ fontWeight: '700' })
    expect(within(screen.getByText('Total').closest('tr')).getByText('3')).toBeInTheDocument()
    expect(container.textContent).not.toContain('pts')
    fireEvent.click(screen.getByRole('button', { name: 'View S001 on canvas' }))
    expect(jump).toHaveBeenCalledWith('S001')
    expect(screen.getByLabelText('Updated result')).toHaveTextContent('Updated')
  })
  it('omits missing tiles and controls, follows check outcome, and dims stale values', () => {
    const { container, rerender } = render(<ResultPanel result={stringResult} stale />)
    const summary = screen.getByLabelText('String result summary')
    expect(within(summary).getByText('STRINGS')).toBeInTheDocument()
    expect(within(summary).getByText('LIMIT 600 V')).toBeInTheDocument()
    expect(within(summary).queryByText('PANELS')).toBeNull()
    expect(within(summary).getByText('Pass')).toHaveStyle({ color: 'var(--status-success)' })
    expect(screen.getByLabelText('Stale result')).toHaveTextContent('Stale')
    expect([...container.querySelectorAll('.string-stat-value')].every(e => e.style.opacity === '0.5')).toBe(true)
    expect(screen.queryByRole('button', { name: /on canvas/ })).toBeNull()
    rerender(<ResultPanel result={{ ok: true, result: { electrical: { pass: false } } }} />)
    expect(screen.getByText('Fail')).toHaveStyle({ color: 'var(--status-danger)' })
    expect(container.querySelectorAll('.string-stat-value')).toHaveLength(0)
  })
})
