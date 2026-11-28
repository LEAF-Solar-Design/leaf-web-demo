// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import PromptBox from './PromptBox.jsx'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('shows the live operand in both the caret and placeholder, and restores the idle bar', () => {
  const props = { value: '', onChange: vi.fn(), onDispatch: vi.fn(), commandLine: true, mcpDiscoveryEnabled: false }
  const { container, rerender } = render(<PromptBox {...props} />)
  const idle = container.innerHTML
  for (const armedAsk of ['LINE  Specify first point:', 'LINE  Specify next point:', 'CIRCLE  Specify radius:']) {
    rerender(<PromptBox {...props} armedAsk={armedAsk} />)
    expect(container.querySelector('.bar-caret').textContent).toBe(armedAsk)
    expect(screen.getByLabelText('Command bar').getAttribute('placeholder')).toBe(armedAsk)
  }
  rerender(<PromptBox {...props} />)
  expect(container.innerHTML).toBe(idle)
})

it('grows a wrapped command and restores the CSS height for a short command', () => {
  let scrollHeight = 84
  vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => scrollHeight)
  vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(21)
  const inputRef = { current: null }
  const props = { onChange: vi.fn(), onDispatch: vi.fn(), commandLine: true, mcpDiscoveryEnabled: false, inputRef }
  const { container, rerender } = render(<PromptBox {...props} value={'A long command sentence '.repeat(14)} />)
  const field = screen.getByLabelText('Command bar')
  expect(inputRef.current).toBe(field)
  expect(container.querySelector('.bar')).toHaveAttribute('data-wrapped', 'true')
  expect(field.style.height).toBe('84px')

  scrollHeight = 21
  rerender(<PromptBox {...props} value="Line" />)
  expect(container.querySelector('.bar')).not.toHaveAttribute('data-wrapped')
  expect(field.style.height).toBe('')
})

it('forwards the textarea to a callback ref', () => {
  const inputRef = vi.fn()
  const { unmount } = render(<PromptBox value="" onChange={vi.fn()} onDispatch={vi.fn()} inputRef={inputRef} />)
  expect(inputRef).toHaveBeenLastCalledWith(screen.getByLabelText('Command bar'))
  unmount()
  expect(inputRef).toHaveBeenLastCalledWith(null)
})
