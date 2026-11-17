import React from 'react'
import { act, fireEvent, within } from '@testing-library/react'
import { renderToString } from 'react-dom/server'
import { hydrateRoot } from 'react-dom/client'
import { expect, it, vi } from 'vitest'
import ModuleSetPicker from './ModuleSetPicker.jsx'

it('hydrates two pickers with stable unique labels and isolated exact callbacks', async () => {
  const data = { sets: [{ set_id: 'opaque-גג', layer: 'Roof', block: 'Panel', count: 2 }] }
  const onA = vi.fn(), onB = vi.fn(), recovered = vi.fn()
  const tree = <>
    <div data-testid="hydrate-a"><ModuleSetPicker drawingKey="A" data={data} onSelect={onA} /></div>
    <div data-testid="hydrate-b"><ModuleSetPicker drawingKey="B" data={data} onSelect={onB} /></div>
  </>
  const container = document.createElement('div')
  document.body.append(container)
  container.innerHTML = renderToString(tree, { identifierPrefix: 'x04-' })
  const before = [...container.querySelectorAll('input[type="checkbox"]')].map((input) => input.id)
  const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
  let root
  try {
    await act(async () => { root = hydrateRoot(container, tree, { identifierPrefix: 'x04-', onRecoverableError: recovered }) })
    const inputs = [...container.querySelectorAll('input[type="checkbox"]')]
    expect(inputs.map((input) => input.id)).toEqual(before)
    expect(new Set(before).size).toBe(2)
    inputs.forEach((input) => {
      const label = input.closest('label')
      expect(label.control).toBe(input)
      expect(label.htmlFor).toBe(input.id)
    })
    const a = within(container.querySelector('[data-testid="hydrate-a"]'))
    const b = within(container.querySelector('[data-testid="hydrate-b"]'))
    fireEvent.click(a.getByRole('checkbox'))
    expect(b.getByRole('checkbox').checked).toBe(false)
    fireEvent.click(a.getByRole('button'))
    expect(onA).toHaveBeenCalledExactlyOnceWith(['opaque-גג'])
    expect(onB).not.toHaveBeenCalled()
    fireEvent.click(b.getByRole('checkbox')); fireEvent.click(b.getByRole('button'))
    expect(onB).toHaveBeenCalledExactlyOnceWith(['opaque-גג'])
    expect(recovered).not.toHaveBeenCalled()
    expect(errors).not.toHaveBeenCalled()
  } finally {
    if (root) await act(async () => { root.unmount() })
    container.remove()
    errors.mockRestore()
  }
})
