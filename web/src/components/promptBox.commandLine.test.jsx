// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import PromptBox from './PromptBox.jsx'

afterEach(cleanup)

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
