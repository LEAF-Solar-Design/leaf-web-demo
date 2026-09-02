import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ToolsPanel from './ToolsPanel.jsx'

afterEach(cleanup)

const TOOLS = [
  { name: 'count-by-layer', description: 'Count entities by layer.', capabilities: ['drawing.read'], kind: 'query', params: { properties: {} } },
]

describe('ToolsPanel read-only browse (no drawing open)', () => {
  it('still lists and opens tools so the catalog is genuinely browsable', () => {
    render(<ToolsPanel tools={TOOLS} running={false} onRequestRun={() => {}} runDisabled runDisabledNote="Upload a DWG or DXF to run this tool." />)
    expect(screen.getByText('count-by-layer')).toBeTruthy()
    fireEvent.click(screen.getByText('count-by-layer'))
    expect(screen.getByRole('button', { name: 'Review & run' })).toBeTruthy()
  })

  it('disables the run control and shows the no-drawing reason instead of the write-lock notes', () => {
    const onRequestRun = vi.fn()
    render(
      <ToolsPanel
        tools={TOOLS}
        running={false}
        onRequestRun={onRequestRun}
        runDisabled
        runDisabledNote="Upload a DWG or DXF to run this tool."
        writeLocked
        writeLockNote="another user holds the edit lock."
      />,
    )
    fireEvent.click(screen.getByText('count-by-layer'))
    const runButton = screen.getByRole('button', { name: 'Review & run' })
    expect(runButton.disabled).toBe(true)
    expect(runButton.title).toBe('Upload a DWG or DXF to run this tool.')
    fireEvent.click(runButton)
    expect(onRequestRun).not.toHaveBeenCalled()
    expect(screen.getByText('Upload a DWG or DXF to run this tool.')).toBeTruthy()
    expect(screen.queryByText(/edit lock/)).toBeNull()
  })

  it('keeps a custom-authored tool browse-only until a drawing is open', () => {
    const onReviseTool = vi.fn()
    render(
      <ToolsPanel
        tools={TOOLS}
        running={false}
        onRequestRun={() => {}}
        onReviseTool={onReviseTool}
        runDisabled
        runDisabledNote="Upload a DWG or DXF to run this tool."
      />,
    )
    fireEvent.click(screen.getByText('count-by-layer'))
    const reviseButton = screen.getByRole('button', { name: 'Revise' })
    expect(reviseButton.disabled).toBe(true)
    expect(reviseButton.title).toBe('Upload a DWG or DXF to run this tool.')
    fireEvent.click(reviseButton)
    expect(onReviseTool).not.toHaveBeenCalled()
  })

  it('runs normally once a drawing makes the tool operable', () => {
    const onRequestRun = vi.fn()
    render(<ToolsPanel tools={TOOLS} running={false} onRequestRun={onRequestRun} runDisabled={false} />)
    fireEvent.click(screen.getByText('count-by-layer'))
    const runButton = screen.getByRole('button', { name: 'Review & run' })
    expect(runButton.disabled).toBe(false)
    fireEvent.click(runButton)
    expect(onRequestRun).toHaveBeenCalledWith(TOOLS[0], {})
  })
})
