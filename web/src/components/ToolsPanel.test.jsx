import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import ToolsPanel from './ToolsPanel.jsx'

afterEach(cleanup)

it('FORM20 closing and switching forms resets drafts and isolates validity', () => {
  const tool = { name: 'edit-json', params: { properties: { changes: { type: 'object', default: { tilt: 10 } } } } }
  const other = { name: 'other-json', params: { properties: { handles: { type: 'array' } } } }
  const onRequestRun = vi.fn()
  render(<ToolsPanel tools={[tool, other]} running={false} onRequestRun={onRequestRun} />)
  fireEvent.click(screen.getByText(tool.name))
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
  const run = () => screen.getByRole('button', { name: 'Review & run' })
  expect(run().disabled).toBe(true)
  fireEvent.click(run()); expect(onRequestRun).not.toHaveBeenCalled()
  fireEvent.click(screen.getByText(tool.name)); fireEvent.click(screen.getByText(tool.name))
  expect(screen.getByLabelText('Changes').value).toBe('{"tilt":10}')
  expect(run().disabled).toBe(false)
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
  fireEvent.click(screen.getByText(other.name))
  expect(run().disabled).toBe(false)
  fireEvent.click(run()); expect(onRequestRun).toHaveBeenCalledWith(other, {})
})

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
