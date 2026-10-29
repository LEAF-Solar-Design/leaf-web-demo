// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ToolExplanation from './ToolExplanation.jsx'
import { explainTool, NOT_PROVIDED } from '../lib/toolExplanation.js'

const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  cleanup()
  if (clipboardDescriptor) Object.defineProperty(navigator, 'clipboard', clipboardDescriptor)
  else delete navigator.clipboard
})

function clipboard(value) {
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value })
}

const tool = {
  name: 'set-layer', description: 'set the layer of selected entities.', capabilities: ['drawing.write'],
  params: { required: ['layer'], properties: { layer: { type: 'string', description: 'Layer name' } } },
  returns_schema: { description: 'Updated entity count' }, provenance: { author: 'agent' },
  review: { status: 'pending' }, timeout_ms: 3000, idempotent: true,
}

describe('ToolExplanation', () => {
  it('renders the disclosure, record sections and explicit gaps', () => {
    const { container } = render(<ToolExplanation tool={tool} />)
    const summary = screen.getByText('How this tool works')
    expect(summary.tagName).toBe('SUMMARY')
    expect(summary.parentElement.className).toBe('tool-explain')
    fireEvent.click(summary)
    expect(screen.getByText('Set the layer of selected entities.')).toBeTruthy()
    expect(screen.getByText('Edits the open drawing').className).toBe('tool-explain-effect write')
    expect(screen.getByText('Inputs')).toBeTruthy()
    expect(screen.getByText('layer (string, required): Layer name')).toBeTruthy()
    expect(screen.getByText('Outputs: Updated entity count')).toBeTruthy()
    expect(screen.getByText('Provenance')).toBeTruthy()
    expect(screen.getByText('Agent-authored')).toBeTruthy()
    expect(screen.getByText('pending')).toBeTruthy()
    expect(screen.getByText('Limits')).toBeTruthy()
    expect(screen.getByText('3 s')).toBeTruthy()
    expect(screen.getByText('Yes')).toBeTruthy()
    expect(container.querySelectorAll('dl')).toHaveLength(2)
    expect(screen.getByText('Not provided by this record')).toBeTruthy()
    for (const item of NOT_PROVIDED) expect(screen.getByText(item)).toBeTruthy()
  })

  it('shows no inputs and omits undeclared limits for a read tool', () => {
    render(<ToolExplanation tool={{ name: 'read' }} />)
    expect(screen.getByText('No inputs')).toBeTruthy()
    expect(screen.getByText('Reads the drawing; does not change it').className).toBe('tool-explain-effect read')
    expect(screen.queryByText('Limits')).toBeNull()
  })

  it('copies the exact explanation text and acknowledges success', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    clipboard({ writeText })
    render(<ToolExplanation tool={tool} />)
    fireEvent.click(screen.getByText('How this tool works'))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy explanation' })) })
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith(explainTool(tool).text)
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
  })

  it.each([undefined, {}, { writeText: true }])('disables copying when writeText is unavailable (%j)', (value) => {
    clipboard(value)
    render(<ToolExplanation tool={tool} />)
    fireEvent.click(screen.getByText('How this tool works'))
    const button = screen.getByRole('button', { name: 'Copy explanation' })
    expect(button.disabled).toBe(true)
    expect(button.title).toBe('Copy is not available here')
  })

  it('keeps a failed clipboard write from reporting success and allows retry', async () => {
    const writeText = vi.fn().mockRejectedValueOnce(new Error('denied')).mockResolvedValueOnce(undefined)
    clipboard({ writeText })
    render(<ToolExplanation tool={tool} />)
    fireEvent.click(screen.getByText('How this tool works'))
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy explanation' })) })
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy failed; try again' })) })
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
  })
})
