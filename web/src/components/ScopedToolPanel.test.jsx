import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ScopedToolPanel from './ScopedToolPanel.jsx'

afterEach(cleanup)

const SCOPE = { label: 'Cut lists', tools: ['timber-cutlist-preflight', 'timber-cutlist'] }
const TOOLS = [
  { name: 'count-by-layer', description: 'never offered' },
  { name: 'timber-cutlist', description: 'Timber cut list from layer-coded plans.' },
  { name: 'timber-cutlist-preflight', description: 'Preview what timber-cutlist will read.' },
]

describe('ScopedToolPanel', () => {
  it('offers exactly the scoped tools, in scope order, and runs through the caller', () => {
    const onRun = vi.fn()
    render(<ScopedToolPanel scope={SCOPE} tools={TOOLS} hasDrawing busy={false} onRun={onRun} />)
    const names = [...screen.getByTestId('scoped-tool-panel').querySelectorAll('.tc-scoped-tool-name')].map((el) => el.textContent)
    expect(names).toEqual(['timber-cutlist-preflight', 'timber-cutlist'])
    expect(screen.queryByText('count-by-layer')).toBeNull()
    expect(screen.queryByText('never offered')).toBeNull()
    fireEvent.click(screen.getAllByRole('button', { name: 'Run' })[1])
    expect(onRun).toHaveBeenCalledWith(TOOLS[1])
  })
  it('keeps Run disabled until a drawing is loaded and while busy', () => {
    const { rerender } = render(<ScopedToolPanel scope={SCOPE} tools={TOOLS} hasDrawing={false} busy={false} onRun={() => {}} />)
    expect(screen.getByText(/Upload a DWG or DXF/)).toBeTruthy()
    for (const button of screen.getAllByRole('button', { name: 'Run' })) expect(button.disabled).toBe(true)
    rerender(<ScopedToolPanel scope={SCOPE} tools={TOOLS} hasDrawing busy onRun={() => {}} />)
    for (const button of screen.getAllByRole('button', { name: 'Run' })) expect(button.disabled).toBe(true)
  })
  it('says so when the scope names nothing the catalog serves', () => {
    render(<ScopedToolPanel scope={{ label: 'Empty', tools: ['ghost'] }} tools={TOOLS} hasDrawing busy={false} onRun={() => {}} />)
    expect(screen.getByTestId('scoped-tool-empty')).toBeTruthy()
    expect(screen.queryAllByRole('button', { name: 'Run' })).toHaveLength(0)
  })
})
