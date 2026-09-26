import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import SchemaForm, { defaultsOf, sentence, valueMatchesSchemaType } from './SchemaForm.jsx'

afterEach(cleanup)

it('SchemaForm renders the moved ParamForm fields for each schema type', () => {
  const schema = { properties: {
    title: { type: 'string', default: 'Roof' },
    spacing: { type: 'number', default: 1.5 },
    count: { type: 'integer', default: 2 },
    enabled: { type: 'boolean', default: true },
    handles: { type: 'array', default: ['A'] },
    changes: { type: 'object', default: { tilt: 10 } },
    expected_rev: { type: ['integer', 'null'], default: null },
    invalid_default: { type: 'integer', default: 'wrong' },
  } }
  const onChange = vi.fn()
  function Form() {
    const [values, setValues] = useState(() => defaultsOf(schema))
    return <SchemaForm schema={schema} values={values} onChange={(next) => { setValues(next); onChange(next) }} />
  }
  render(<Form />)
  expect(screen.getByLabelText('Title').value).toBe('Roof')
  expect(screen.getByLabelText('Spacing').type).toBe('number')
  expect(screen.getByLabelText('Spacing').value).toBe('1.5')
  expect(screen.getByLabelText('Count').step).toBe('1')
  expect(screen.getByLabelText('Count').value).toBe('2')
  expect(screen.getByLabelText('Enabled').checked).toBe(true)
  expect(screen.getByLabelText('Handles').value).toBe('["A"]')
  expect(screen.getByLabelText('Changes').value).toBe('{"tilt":10}')
  expect(screen.getByLabelText('Expected rev').value).toBe('')
  expect(screen.getByLabelText('Invalid default').value).toBe('0')
  expect(defaultsOf(schema)).not.toHaveProperty('invalid_default')
  expect(valueMatchesSchemaType(1.5, { type: 'integer' })).toBe(false)
  expect(sentence('solar input')).toBe('Solar input')

  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'South roof' } })
  fireEvent.change(screen.getByLabelText('Spacing'), { target: { value: '2.5' } })
  fireEvent.change(screen.getByLabelText('Count'), { target: { value: '3' } })
  fireEvent.click(screen.getByLabelText('Enabled'))
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '4' } })
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Handles'), { target: { value: '["B","C"]' } })
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{"tilt":20}' } })
  expect(onChange).toHaveBeenLastCalledWith({
    title: 'South roof', spacing: 2.5, count: 3, enabled: false,
    handles: ['B', 'C'], changes: { tilt: 20 }, expected_rev: null,
  })
  onChange.mockClear()
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{' } })
  expect(screen.getByLabelText('Changes').getAttribute('aria-invalid')).toBe('true')
  fireEvent.change(screen.getByLabelText('Handles'), { target: { value: '{}' } })
  expect(screen.getByLabelText('Handles').getAttribute('aria-invalid')).toBe('true')
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{}' } })
  expect(screen.getByLabelText('Changes').getAttribute('aria-invalid')).toBe('false')
  expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ changes: {} }))
})

it('SchemaForm says No parameters for an empty schema', () => {
  const { rerender } = render(<SchemaForm schema={{ properties: {} }} values={{}} onChange={() => {}} />)
  expect(screen.getByText('No parameters.').className).toBe('params-none')
  expect(screen.queryByRole('textbox')).toBeNull()
  rerender(<SchemaForm values={{}} onChange={() => {}} />)
  expect(screen.getByText('No parameters.')).toBeTruthy()
})
