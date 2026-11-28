// @vitest-environment jsdom
import { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { BUILD_TOOL_STARTER } from '../lib/starterPrompts.js'
import PromptBox from './PromptBox.jsx'

afterEach(cleanup)

function Composer({ starter = null, initialValue = '', onChange = vi.fn(), onDispatch = vi.fn() }) {
  const [value, setValue] = useState(initialValue)
  return <PromptBox value={value} starter={starter} mcpDiscoveryEnabled={false}
    onChange={(text) => { setValue(text); onChange(text) }} onDispatch={onDispatch} />
}

describe('command bar first request starter', () => {
  it('renders no starter by default', () => {
    render(<Composer />)
    expect(screen.queryByTestId('prompt-starter')).not.toBeInTheDocument()
  })

  it('fills the controlled prompt and selects its name without dispatching', () => {
    const onChange = vi.fn()
    const onDispatch = vi.fn()
    render(<Composer starter={BUILD_TOOL_STARTER} initialValue="  " onChange={onChange} onDispatch={onDispatch} />)
    const starter = screen.getByTestId('prompt-starter')
    expect(starter).toHaveTextContent('Build an AutoCAD tool')
    expect(starter.previousElementSibling).toHaveClass('bar-input')
    fireEvent.click(starter)
    expect(onChange).toHaveBeenCalledExactlyOnceWith(BUILD_TOOL_STARTER.text)
    const field = screen.getByRole('combobox', { name: 'Command bar' })
    expect(field).toHaveValue('Build an AutoCAD tool, name: ')
    expect(field).toHaveFocus()
    expect(field.selectionStart).toBe(23)
    expect(field.selectionEnd).toBe(27)
    expect(onDispatch).not.toHaveBeenCalled()
    expect(screen.queryByTestId('prompt-starter')).not.toBeInTheDocument()
    fireEvent.change(field, { target: { value: '' } })
    expect(screen.getByTestId('prompt-starter')).toBeInTheDocument()
  })

  it('offers the starter for an empty value and hides it for a typed request', () => {
    render(<Composer starter={BUILD_TOOL_STARTER} />)
    expect(screen.getByTestId('prompt-starter')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: 'Command bar' }), { target: { value: 'My request' } })
    expect(screen.queryByTestId('prompt-starter')).not.toBeInTheDocument()
  })
})
