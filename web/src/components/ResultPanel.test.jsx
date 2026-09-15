import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import ResultPanel from './ResultPanel.jsx'


afterEach(cleanup)

describe('ResultPanel outcome fallbacks', () => {
  it('does not call highlight overlay output empty', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: {}, overlay: { highlight_handles: ['A1'] } }} />)
    expect(screen.queryByText('Completed with no output.')).toBeNull()
    expect(screen.getByText('1 panel highlighted in the viewer')).toBeInTheDocument()
  })

  it('does not call files-only output empty', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: { files: [{ name: 'a.dxf' }] } }} />)
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('reports a successful run with no result', () => {
    render(<ResultPanel running={false} result={{ ok: true }} />)
    expect(screen.getByText('Completed with no output.')).toBeInTheDocument()
  })

  it('reports a nested-only result without an empty key/value table', () => {
    const { container } = render(<ResultPanel running={false} result={{ ok: true, result: { nested: { a: 1 } } }} />)
    expect(screen.getByText('Completed with no output.')).toBeInTheDocument()
    expect(container.querySelector('table.kv')).toBeNull()
  })

  it('reports a failed run without a reason', () => {
    render(<ResultPanel running={false} result={{ ok: false }} />)
    expect(screen.getByText('The run failed without reporting a reason.')).toBeInTheDocument()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })

  it('keeps the counts table for counts output', () => {
    render(<ResultPanel running={false} result={{ ok: true, result: { counts: { Panels: 4 }, total: 4 } }} />)
    expect(screen.getByRole('table')).toHaveClass('counts')
    expect(screen.getByRole('cell', { name: 'Panels' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '4' })).toBeInTheDocument()
    expect(screen.queryByText('Completed with no output.')).toBeNull()
  })
})


describe('ResultPanel actionable error guidance', () => {
  it('shows the next action and responsible actor from the shared envelope', () => {
    render(
      <ResultPanel
        running={false}
        tool={{ name: 'drape-onto-spheres' }}
        result={{
          ok: false,
          tool: 'drape-onto-spheres',
          version: '1.0.1',
          error: {
            error_code: 'ENTITLEMENT_REQUIRED',
            message: 'This workspace cannot run drawing-write tools.',
            retryable: false,
            retry_class: 'after_action',
            actor: 'workspace_admin',
            next_action: 'Enable this capability for the workspace, then retry.',
          },
          timing_ms: 0,
          cost: null,
          entitlement_required: 'drawing.write',
        }}
      />,
    )

    expect(screen.getByText(
      'Next: Enable this capability for the workspace, then retry.',
    )).toBeInTheDocument()
    expect(screen.getByText('Workspace admin')).toBeInTheDocument()
  })
})
