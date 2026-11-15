import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import WidgetsView from './WidgetsView.jsx'

afterEach(cleanup)

it.each([
  ['multiline Unicode', '\u03b1\u{1F600}e\u0301\n"quotes"\\tail'],
  ['more than forty UTF16 units', '\u{1F600}e\u0301"\\'.repeat(12)],
  ['complete emoji at the contract limit', 'a'.repeat(510) + '\u{1F600}'],
  ['JSON escape characters', '"quoted" \\ tab\t & <tag> \u00e9'],
])('retains exact %s in the visible text editor', (label, text) => {
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'unicode-proof' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Edit full text', exact: true }))
  fireEvent.change(screen.getByLabelText('Full text', { exact: true }), { target: { value: text } })
  expect(screen.getByLabelText('Full text', { exact: true })).toHaveValue(text)
  expect(screen.getByRole('img', { name: 'Widget preview', exact: true }).querySelector('text').textContent).toBe(text)
})

it('keeps the short text limit and changes editor mode without changing document text', () => {
  render(<WidgetsView />)
  fireEvent.change(screen.getByLabelText('Document ID'), { target: { value: 'short-text-proof' } })
  fireEvent.click(screen.getByRole('button', { name: 'New', exact: true }))
  fireEvent.click(screen.getByRole('button', { name: 'Add text', exact: true }))
  const value = screen.getByLabelText('Text').value
  expect(screen.getByLabelText('Text')).toHaveAttribute('maxlength', '40')
  fireEvent.click(screen.getByRole('button', { name: 'Edit full text', exact: true }))
  expect(screen.getByLabelText('Full text')).toHaveAttribute('maxlength', '512')
  expect(screen.getByLabelText('Full text')).toHaveValue(value)
  fireEvent.click(screen.getByRole('button', { name: 'Use short text', exact: true }))
  expect(screen.getByLabelText('Text')).toHaveAttribute('maxlength', '40')
  expect(screen.getByLabelText('Text')).toHaveValue(value)
})
