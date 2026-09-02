import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ResultPanel from './ResultPanel.jsx'


afterEach(cleanup)

const envelope = {
  ok: true,
  tool: 'timber-cutlist',
  version: '1.0.0',
  timing_ms: 212,
  cost: null,
  error: null,
  result: {
    member_count: 19,
    view_count: 6,
    table: {
      columns: ['Material/Dim', 'Length (mm)', 'Quantity', 'Notes'],
      rows: [
        ['CLADDING_22X2120', '3000', 8, ''],
        ['BOLT_8', '250', 20, '8mm diameter, spaced at 600mm centres'],
      ],
    },
    warnings: ['layer \'WINDOW\' does not follow Material_W x H: 48 run(s) not counted'],
    files: [
      { name: 'cutlist.csv', mime: 'text/csv', bytes: 5, base64: btoa('a,b,c') },
    ],
  },
}

describe('ResultPanel generic table results', () => {
  it('renders result.table rows, warnings, scalars and file downloads', () => {
    const createObjectURL = vi.fn(() => 'blob:cutlist')
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() })
    render(<ResultPanel running={false} tool={{ name: 'timber-cutlist' }} result={envelope} />)
    expect(screen.getByText('Material/Dim')).toBeInTheDocument()
    expect(screen.getByText('CLADDING_22X2120')).toBeInTheDocument()
    expect(screen.getByText('8mm diameter, spaced at 600mm centres')).toBeInTheDocument()
    expect(screen.getByText(/WINDOW/)).toBeInTheDocument()
    const link = screen.getByText('Download cutlist.csv')
    expect(link.getAttribute('href')).toBe('blob:cutlist')
    expect(link.getAttribute('download')).toBe('cutlist.csv')
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
