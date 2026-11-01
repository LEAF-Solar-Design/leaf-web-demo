// @vitest-environment jsdom
import { useState } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, createEvent, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PromptBox from './PromptBox.jsx'

vi.mock('../site/DrawingObjectsContext.jsx', () => ({
  useDrawingObjects: () => ({
    index: { records: [0, 1, 2].map((i) => ({ id: `h:${i}`, name: `panel ${i}`, handles: [] })) },
    focus: () => {},
  }),
}))

afterEach(cleanup)

function Harness({ initialValue = 'panel', onDispatch, actions }) {
  const [value, setValue] = useState(initialValue)
  return <PromptBox value={value} onChange={setValue} onDispatch={onDispatch}
    mcpDiscoveryEnabled={false} paletteActions={actions} />
}

function mountScope(scope, initialValue = 'panel') {
  const actions = [0, 1, 2].map((i) => ({
    id: `panel-${i}`, label: `panel ${i}`, kbd: `${i + 1}`, onSelect: vi.fn(),
  }))
  const onDispatch = vi.fn()
  const view = render(<Harness initialValue={initialValue} onDispatch={onDispatch} actions={actions} />)
  fireEvent.click(view.container.querySelector('.bar-scope'))
  fireEvent.click(within(screen.getByRole('listbox', { name: 'Scope' })).getByText(scope))
  return { ...view, actions, onDispatch, input: screen.getByRole('combobox') }
}

function expectMounted(input, id) {
  const active = input.getAttribute('aria-activedescendant')
  if (id) expect(active).toBe(id)
  expect(active).toBeTruthy()
  const row = document.getElementById(active)
  expect(row).not.toBeNull()
  expect(row.tagName).toBe('LI')
  expect(row).toHaveAttribute('role', 'option')
  expect(row).toHaveAttribute('aria-selected', 'true')
  return row
}

describe.each(['act', 'find'])('%s result row parity', (scope) => {
  it('puts Ask last, wraps both ways, and keeps every active descendant mounted', () => {
    const { input } = mountScope(scope)
    const menu = screen.getByRole('listbox')
    expect(menu.tagName).toBe('UL')
    const rows = within(menu).getAllByRole('option')
    expect(rows).toHaveLength(4)
    expect(rows.map((row) => row.dataset.kind)).toEqual([
      ...Array(3).fill(scope === 'act' ? 'action' : 'drawing-object'), 'ask',
    ])
    expect(rows[3]).toHaveAttribute('id', 'ask-opt')
    expect(rows[3]).toHaveTextContent('Ask Leaf: "panel"')
    expect(rows[3]).toHaveAccessibleName('Ask Leaf')
    expect(rows[3]).toHaveAttribute('aria-description', 'panel')
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
    expectMounted(input, rows[0].id)
    for (const row of rows.slice(1)) {
      fireEvent.keyDown(input, { key: 'ArrowDown' })
      expectMounted(input, row.id)
    }
    fireEvent.keyDown(input, { key: 'ArrowDown' })
    expectMounted(input, rows[0].id)
    fireEvent.keyDown(input, { key: 'ArrowUp' })
    expectMounted(input, 'ask-opt')
  })

  it.each(['Enter', 'click'])('submits Ask through the composer path with %s', (gesture) => {
    const { input, onDispatch, actions } = mountScope(scope)
    fireEvent.keyDown(input, { key: 'Tab' })
    const ask = expectMounted(input, 'ask-opt')
    if (gesture === 'click') fireEvent.click(ask)
    else fireEvent.keyDown(input, { key: 'Enter' })
    expect(onDispatch).toHaveBeenCalledExactlyOnceWith(undefined, expect.objectContaining({ images: [] }))
    for (const action of actions) expect(action.onSelect).not.toHaveBeenCalled()
    expect(input).toHaveValue('panel')
  })

  it('yields Shift+Enter for native newline insertion and IME Enter for candidate confirmation', () => {
    const { input, onDispatch, actions } = mountScope(scope)
    const newline = createEvent.keyDown(input, { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true })
    fireEvent(input, newline)
    expect(newline.defaultPrevented).toBe(false)
    // jsdom has no native textarea editing; deliver the browser's resulting input.
    fireEvent.change(input, { target: { value: 'panel\n' } })
    expect(input).toHaveValue('panel\n')
    fireEvent.keyDown(input, { key: 'Tab' })
    expectMounted(input, 'ask-opt')
    for (const marks of [{ isComposing: true }, { keyCode: 229 }]) {
      const event = createEvent.keyDown(input, { key: 'Enter', bubbles: true, cancelable: true, ...marks })
      fireEvent(input, event)
      expect(event.defaultPrevented).toBe(false)
      expectMounted(input, 'ask-opt')
    }
    expect(onDispatch).not.toHaveBeenCalled()
    for (const action of actions) expect(action.onSelect).not.toHaveBeenCalled()
  })

  it('keeps the active id valid when results shrink, then clears it when closed', () => {
    const { input } = mountScope(scope)
    fireEvent.keyDown(input, { key: 'Tab' })
    fireEvent.change(input, { target: { value: 'unmatched' } })
    expect(screen.getAllByRole('option')).toHaveLength(1)
    expectMounted(input, 'ask-opt')
    fireEvent.change(input, { target: { value: '' } })
    expect(screen.queryByRole('option', { name: /Ask Leaf/ })).toBeNull()
    if (input.hasAttribute('aria-activedescendant')) expectMounted(input)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).not.toHaveAttribute('aria-activedescendant')
  })
})

it('mounts slash active descendants as list items and renders the Tab keycap', () => {
  render(<PromptBox value="/panel" onChange={() => {}} onDispatch={() => {}}
    mcpDiscoveryEnabled={false} tools={[{ name: 'panel-count' }]} />)
  const input = screen.getByRole('combobox')
  // Position the picker at the end of the controlled initial value.
  input.setSelectionRange(input.value.length, input.value.length)
  fireEvent.select(input)
  const row = expectMounted(input, 'slash-opt-0')
  expect(row.closest('ul')).toHaveAttribute('role', 'listbox')
  expect(within(row).getByText('Tab').tagName).toBe('KBD')
  expect(within(row).getByText('Tab')).toHaveClass('dp-keycap')
  expect(screen.queryByRole('option', { name: /Ask Leaf/ })).toBeNull()
})

it('uses the exact row, selected rail/tint and keycap recipes with instant C2-A highlights', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/components/promptParity.css'), 'utf8')
  expect(css).toContain('/* leaf-dp-v1 */')
  expect(css).toContain('display:flex;align-items:center;justify-content:space-between;gap:var(--leaf-space-3);min-height:var(--leaf-metric-row-compact);padding:0 var(--leaf-space-3);font-size:var(--leaf-size-dense);line-height:var(--leaf-leading-dense);border-left:2px solid transparent')
  expect(css).toMatch(/\.pb-result\[aria-selected="true"\][^{]*\{border-left-color:var\(--leaf-accent\);background:var\(--leaf-accent-tint\)\}/)
  expect(css).toContain('.pb-result .dp-keycap{display:inline-flex;align-items:center;min-width:20px;height:20px;padding:0 var(--leaf-space-1);border:1px solid var(--leaf-line-control);border-radius:var(--leaf-radius-key);font-size:var(--leaf-size-caption);line-height:var(--leaf-leading-caption)}')
  // C2-A: design-parity-forks-20261101/receipts/operator-forks-all-a-20261101.json.
  const rowRules = css.match(/[^{}]*\.pb-result[^{}]*\{[^{}]*\}/g)
  expect(rowRules?.length).toBeGreaterThan(0)
  for (const rule of rowRules) expect(rule).not.toMatch(/transition(?:-[\w-]+)?\s*:/)
})
