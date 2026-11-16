import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

afterEach(cleanup)

// DOM event observation only: this does not emulate an operating-system IME.
it('retains composition-event text at the UTF16 limit and its existing edit history', () => {
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'composition-proof' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Edit full text', exact: true }))
  const before = 'a'.repeat(510), intermediate = before + 'e', committed = intermediate + '\u0301'
  const field = () => screen.getByLabelText('Full text', { exact: true })
  fireEvent.change(field(), { target: { value: before } })
  fireEvent.compositionStart(field())
  fireEvent.change(field(), { target: { value: intermediate } })
  expect(field()).toHaveValue(intermediate)
  fireEvent.compositionUpdate(field(), { data: 'e\u0301' })
  fireEvent.change(field(), { target: { value: committed } })
  fireEvent.compositionEnd(field(), { data: 'e\u0301' })
  expect(committed.length).toBe(512)
  expect(field()).toHaveAttribute('maxlength', '512')
  expect(field()).toHaveValue(committed)
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Save', exact: true })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Undo', exact: true }))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  // Current history records text changes, rather than promising atomic IME undo.
  expect(field()).toHaveValue(intermediate)
  fireEvent.click(screen.getByRole('button', { name: 'Redo', exact: true }))
  fireEvent.click(document.querySelector('button[data-layer-id]'))
  expect(field()).toHaveValue(committed)
  expect(screen.getByRole('img', { name: 'Widget preview', exact: true }).querySelector('text').textContent).toBe(committed)
})
