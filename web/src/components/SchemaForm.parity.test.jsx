import React, { useState } from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SchemaForm from './SchemaForm.jsx'

afterEach(cleanup)

const schema = {
  required: ['name'],
  properties: {
    name: { type: 'string', description: 'Name this roof.' },
    width: { type: 'number', 'x-leaf-unit': 'mm', minimum: 1 },
    changes: { type: 'object' },
  },
}

function Form({ onChange = () => {} }) {
  const [values, setValues] = useState({ name: 'Roof', width: 2 })
  return <form aria-label="Parameters" onSubmit={(event) => event.preventDefault()}>
    <SchemaForm schema={schema} values={values} onChange={(next, metadata) => {
      setValues(next)
      onChange(next, metadata)
    }} />
    <button type="submit">Run</button>
  </form>
}

it('associates stable labels and help, retaining help when an empty required field has an error', () => {
  render(<Form />)
  const name = screen.getByRole('textbox', { name: 'Name', exact: true })
  const wrapper = name.closest('[data-dp-field]')
  expect(name).toHaveAttribute('id', 'sf-name')
  expect(wrapper.querySelector('label')).toHaveAttribute('for', 'sf-name')
  expect(screen.getByText('Name this roof.')).toHaveAttribute('id', 'sf-name-help')
  expect(name).toHaveAttribute('aria-describedby', 'sf-name-help')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()

  fireEvent.change(name, { target: { value: '' } })
  fireEvent.submit(screen.getByRole('form', { name: 'Parameters' }))
  expect(screen.getByRole('alert')).toHaveAttribute('id', 'sf-name-error')
  expect(screen.getByRole('alert')).toHaveTextContent('Name is required.')
  expect(name).toHaveAttribute('aria-describedby', 'sf-name-help sf-name-error')
  expect(name).toHaveAttribute('aria-invalid', 'true')

  fireEvent.change(name, { target: { value: 'Roof' } })
  expect(name).toHaveAttribute('id', 'sf-name')
  expect(name).toHaveAttribute('aria-describedby', 'sf-name-help')
  expect(name).toHaveAttribute('aria-invalid', 'false')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('keeps ids stable when a schema update remounts a field as a select', () => {
  const props = { values: { name: 'Roof' }, onChange: vi.fn() }
  const { rerender } = render(<SchemaForm {...props} schema={schema} />)
  rerender(<SchemaForm {...props} schema={{ properties: {
    name: { ...schema.properties.name, enum: ['Roof', 'Ground'] },
  } }} />)
  const name = screen.getByRole('combobox', { name: 'Name', exact: true })
  expect(name).toHaveAttribute('id', 'sf-name')
  expect(name.labels[0]).toHaveAttribute('for', 'sf-name')
  expect(name).toHaveAttribute('aria-describedby', 'sf-name-help')
})

it('renders a hidden unit suffix only for fields with x-leaf-unit and preserves range hints', () => {
  const { container } = render(<Form />)
  const width = screen.getByRole('spinbutton', { name: 'Width' })
  const wrapper = width.closest('[data-dp-field]')
  expect(wrapper).toHaveClass('has-unit')
  expect(wrapper.querySelector('.sf-unit')).toHaveTextContent('mm')
  expect(wrapper.querySelector('.sf-unit')).toHaveAttribute('aria-hidden', 'true')
  expect(container.querySelectorAll('.sf-unit')).toHaveLength(1)
  expect(screen.getByLabelText('Name').closest('[data-dp-field]')).not.toHaveClass('has-unit')
  expect(width).toHaveAttribute('aria-describedby', 'sf-width-range')
  expect(screen.getByText('Minimum: 1.')).toHaveAttribute('id', 'sf-width-range')
  fireEvent.change(width, { target: { value: '0' } })
  expect(width).toHaveAttribute('aria-describedby', 'sf-width-range sf-width-error')
  expect(width).toHaveAttribute('aria-invalid', 'true')
})

it('keeps malformed JSON in the field without publishing a non-object value', () => {
  const onChange = vi.fn()
  render(<Form onChange={onChange} />)
  const changes = screen.getByLabelText('Changes')
  fireEvent.change(changes, { target: { value: '{bad json' } })
  expect(changes).toHaveValue('{bad json')
  expect(changes).toHaveAttribute('aria-describedby', 'sf-changes-error')
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.change(changes, { target: { value: '[]' } })
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.change(changes, { target: { value: '{"tilt":20}' } })
  expect(onChange).toHaveBeenCalledExactlyOnceWith(
    { name: 'Roof', width: 2, changes: { tilt: 20 } }, { changedKey: 'changes' })
})

it('does not render a positive validation marker for valid or corrected fields', () => {
  render(<Form />)
  expect(screen.queryByText(/Valid|[✓✔✅]/i)).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Roof' } })
  expect(screen.queryByText(/Valid|[✓✔✅]/i)).not.toBeInTheDocument()
})

it('uses the DP control metrics and coarse-pointer rules', () => {
  const css = readFileSync(resolve(process.cwd(), 'src/components/schemaFormParity.css'), 'utf8')
  expect(css.startsWith('/* leaf-dp-v1 */')).toBe(true)
  expect(css).toContain('min-height:var(--leaf-metric-control)')
  expect(css).toContain('@media (pointer:coarse)')
  expect(css).toContain('min-height:var(--leaf-metric-control-touch)')
  expect(css).toContain('font-size:var(--leaf-size-touch)')
})
