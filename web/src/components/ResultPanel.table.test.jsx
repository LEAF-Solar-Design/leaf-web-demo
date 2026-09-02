import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
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
