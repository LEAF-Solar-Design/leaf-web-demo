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
  expect(screen.getByLabelText('Enabled').value).toBe('1')
  expect(screen.getByLabelText('Handles').value).toBe('["A"]')
  expect(screen.getByLabelText('Changes').value).toBe('{"tilt":10}')
  expect(screen.getByLabelText('Expected rev').value).toBe('')
  expect(screen.getByLabelText('Invalid default').value).toBe('')
  expect(defaultsOf(schema)).not.toHaveProperty('invalid_default')
  expect(valueMatchesSchemaType(1.5, { type: 'integer' })).toBe(false)
  expect(sentence('solar input')).toBe('Solar input')

  fireEvent.change(screen.getByLabelText('Title'), { target: { value: 'South roof' } })
  fireEvent.change(screen.getByLabelText('Spacing'), { target: { value: '2.5' } })
  fireEvent.change(screen.getByLabelText('Count'), { target: { value: '3' } })
  fireEvent.change(screen.getByLabelText('Enabled'), { target: { value: '0' } })
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '4' } })
  fireEvent.change(screen.getByLabelText('Expected rev'), { target: { value: '' } })
  fireEvent.change(screen.getByLabelText('Handles'), { target: { value: '["B","C"]' } })
  fireEvent.change(screen.getByLabelText('Changes'), { target: { value: '{"tilt":20}' } })
  expect(onChange).toHaveBeenLastCalledWith({
    title: 'South roof', spacing: 2.5, count: 3, enabled: false,
    handles: ['B', 'C'], changes: { tilt: 20 },
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

function mountForm(properties, extra = {}) {
  const schema = { properties, ...extra }
  const submit = vi.fn()
  function Host() {
    const [values, setValues] = useState(() => defaultsOf(schema))
    const [valid, setValid] = useState(true)
    return <><SchemaForm schema={schema} values={values} onChange={(next) => setValues(next)} onValidityChange={setValid} />
      <button disabled={!valid} onClick={() => { if (valid) submit(values) }}>Run</button></>
  }
  render(<Host />)
  return submit
}
const field = (label) => screen.getByLabelText(label)
const edit = (label, value) => fireEvent.change(field(label), { target: { value } })
const runForm = () => fireEvent.click(screen.getByRole('button', { name: 'Run' }))
const blockedForm = () => expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(true)

it('FORM01 required marks keep the exact control name', () => {
  mountForm({ name: { type: 'string' } }, { required: ['name'] })
  expect(screen.getByText('*')).toBeTruthy()
  expect(screen.getByRole('textbox', { name: 'Name', exact: true })).toBe(field('Name'))
  expect(field('Name').getAttribute('aria-required')).toBe('true')
})

it('FORM02 descriptions are visible and describe the field', () => {
  mountForm({ name: { type: 'string', description: 'Name this roof.' } })
  const description = screen.getByText('Name this roof.')
  expect(field('Name').getAttribute('aria-describedby')).toBe(description.id)
})

it('FORM03 string enums submit the selected typed option', () => {
  const submit = mountForm({ mode: { type: 'string', enum: ['roof', 'ground'] } })
  expect([...field('Mode').options].map((option) => option.text)).toEqual(['Choose a value', 'roof', 'ground'])
  edit('Mode', '1'); runForm()
  expect(submit).toHaveBeenCalledWith({ mode: 'ground' })
  edit('Mode', ''); runForm()
  expect(submit).toHaveBeenLastCalledWith({})
})

it('FORM04 integer enums preserve numbers', () => {
  const submit = mountForm({ count: { type: 'integer', enum: [1, 2] } })
  edit('Count', '0'); runForm()
  expect(submit).toHaveBeenCalledWith({ count: 1 })
})

it('FORM05 inclusive integer bounds are visible', () => {
  mountForm({ count: { type: 'integer', minimum: 1, maximum: 5 } })
  expect(field('Count').min).toBe('1'); expect(field('Count').max).toBe('5')
  expect(field('Count').step).toBe('1')
  expect(screen.getByText('Minimum: 1. Maximum: 5.')).toBeTruthy()
})

it('FORM06 strict number bounds block endpoints and allow an interior value', () => {
  const submit = mountForm({ ratio: { type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 } })
  expect(field('Ratio').min).toBe('0'); expect(field('Ratio').max).toBe('1')
  expect(field('Ratio').step).toBe('any')
  expect(screen.getByText('Must be greater than 0 and less than 1.')).toBeTruthy()
  edit('Ratio', '0'); blockedForm(); runForm()
  expect(field('Ratio').validationMessage).toBe('Ratio is outside its allowed range.')
  expect(submit).not.toHaveBeenCalled()
  edit('Ratio', '0.5'); runForm()
  expect(submit).toHaveBeenCalledWith({ ratio: 0.5 })
})

it('FORM07 legacy strict integer bounds use whole number endpoints', () => {
  const submit = mountForm({ count: { type: 'integer', minimum: 1, maximum: 5, exclusiveMinimum: true, exclusiveMaximum: true } })
  expect(field('Count').min).toBe('2'); expect(field('Count').max).toBe('4')
  for (const value of ['1', '5']) { edit('Count', value); blockedForm(); runForm() }
  expect(submit).not.toHaveBeenCalled()
})

it('FORM08 untouched optional values stay empty and omitted', () => {
  const submit = mountForm({ n: { type: 'integer' }, o: { type: 'object' }, a: { type: 'array' }, s: { type: 'string' } })
  for (const label of ['N', 'O', 'A', 'S']) expect(field(label).value).toBe('')
  runForm(); expect(submit).toHaveBeenCalledWith({})
})

it('FORM09 supplied defaults including falsy values survive', () => {
  const submit = mountForm({ n: { type: 'integer', default: 0 }, b: { type: 'boolean', default: false },
    s: { type: 'string', default: 'x' }, o: { type: 'object', default: {} }, empty: { type: 'string', default: '' } })
  expect(field('N').value).toBe('0'); expect(field('B').value).toBe('0')
  expect(field('S').value).toBe('x'); expect(field('O').value).toBe('{}')
  runForm(); expect(submit).toHaveBeenCalledWith({ n: 0, b: false, s: 'x', o: {}, empty: '' })
})

it('FORM10 an optional boolean distinguishes false from unset', () => {
  const submit = mountForm({ flag: { type: 'boolean' } })
  expect(field('Flag').value).toBe('')
  expect([...field('Flag').options].map((option) => option.text)).toEqual(['Choose a value', 'False', 'True'])
  runForm(); expect(submit).toHaveBeenLastCalledWith({})
  edit('Flag', '0'); runForm(); expect(submit).toHaveBeenLastCalledWith({ flag: false })
})

it('FORM11 clearing a string default omits it', () => {
  const submit = mountForm({ name: { type: 'string', default: 'roof' } })
  edit('Name', 'ground'); edit('Name', ''); runForm()
  expect(field('Name').value).toBe(''); expect(submit).toHaveBeenCalledWith({})
})

it('FORM12 clearing a nullable number omits it', () => {
  const submit = mountForm({ size: { type: ['number', 'null'], default: 3 } })
  edit('Size', '4'); edit('Size', ''); runForm()
  expect(field('Size').value).toBe(''); expect(submit).toHaveBeenCalledWith({})
})

it('FORM13 malformed JSON stays visible and blocks execution', () => {
  const submit = mountForm({ changes: { type: 'object', default: {} } })
  edit('Changes', '{'); runForm(); blockedForm()
  expect(field('Changes').value).toBe('{')
  const error = screen.getByRole('alert')
  expect(error.textContent).toBe('Changes must contain valid JSON.')
  expect(field('Changes').getAttribute('aria-invalid')).toBe('true')
  expect(field('Changes').getAttribute('aria-describedby')).toBe(error.id)
  expect(submit).not.toHaveBeenCalled()
})

it('FORM14 arrays cannot satisfy an object field', () => {
  const submit = mountForm({ changes: { type: 'object' } })
  edit('Changes', '[]'); blockedForm(); runForm()
  expect(screen.getByRole('alert').textContent).toBe('Changes must contain a JSON object.')
  expect(submit).not.toHaveBeenCalled()
})

it('FORM15 objects cannot satisfy an array field', () => {
  const submit = mountForm({ handles: { type: 'array' } })
  edit('Handles', '{}'); blockedForm(); runForm()
  expect(screen.getByRole('alert').textContent).toBe('Handles must contain a JSON array.')
  expect(submit).not.toHaveBeenCalled()
})

it('FORM16 correcting JSON removes the error and submits an object', () => {
  const submit = mountForm({ changes: { type: 'object' }, note: { type: 'string' } })
  edit('Changes', '{'); edit('Note', 'rerender')
  expect(field('Changes').value).toBe('{'); blockedForm()
  edit('Note', ''); edit('Changes', '{"tilt":20}'); runForm()
  expect(screen.queryByRole('alert')).toBeNull()
  expect(submit).toHaveBeenCalledWith({ changes: { tilt: 20 } })
  cleanup()
  const schema = { properties: { changes: { type: 'object' }, expected_rev: { type: 'integer' } } }
  const onChange = vi.fn(), onValidityChange = vi.fn()
  const props = { schema, values: { changes: {} }, onChange, onValidityChange }
  const view = render(<SchemaForm {...props} />)
  edit('Changes', '{')
  view.rerender(<SchemaForm {...props} values={{ changes: {}, expected_rev: 7 }} />)
  expect(field('Changes').value).toBe('{')
  expect(field('Expected rev').value).toBe('7')
  expect(onValidityChange).toHaveBeenLastCalledWith(false)
  view.rerender(<SchemaForm {...props} values={{ changes: { tilt: 30 } }} />)
  expect(field('Changes').value).toBe('{"tilt":30}')
  expect(screen.queryByRole('alert')).toBeNull()
  edit('Changes', '')
  expect(onChange).toHaveBeenLastCalledWith({}, { changedKey: 'changes' })
  edit('Changes', '{')
  view.rerender(<SchemaForm {...props} values={{}} />)
  expect(field('Changes').value).toBe('')
  expect(onValidityChange).toHaveBeenLastCalledWith(true)
  edit('Changes', '{')
  view.rerender(<SchemaForm {...props} values={{}} schema={{ properties: { changes: { type: 'array' } } }} />)
  expect(field('Changes').value).toBe('')
  expect(screen.queryByRole('alert')).toBeNull()
  expect(onValidityChange).toHaveBeenLastCalledWith(true)
})

it('FORM17 nested objects stay a single JSON field', () => {
  const submit = mountForm({ config: { type: 'object', properties: { roof: { type: 'object' } } } })
  expect(screen.getAllByRole('textbox')).toHaveLength(1)
  edit('Config', '{"roof":{"tilt":10}}'); runForm()
  expect(submit).toHaveBeenCalledWith({ config: { roof: { tilt: 10 } } })
})

it('FORM18 nullable JSON defaults display and submit null', () => {
  const submit = mountForm({ config: { type: ['object', 'null'], default: null }, size: { type: ['number', 'null'], default: null } })
  expect(field('Config').value).toBe('null'); expect(field('Size').value).toBe('')
  expect(screen.getByText('Current value: null.')).toBeTruthy()
  runForm(); expect(submit).toHaveBeenLastCalledWith({ config: null, size: null })
  edit('Config', '[]'); expect(screen.getByRole('alert').textContent).toBe('Config must contain a JSON object or null.')
  edit('Config', 'null'); runForm(); expect(submit).toHaveBeenLastCalledWith({ config: null, size: null })
  edit('Size', '2'); edit('Size', ''); edit('Config', ''); runForm()
  expect(screen.queryByText('Current value: null.')).toBeNull()
  expect(submit).toHaveBeenLastCalledWith({})
})

it('FORM19 correcting only one invalid field keeps execution blocked', () => {
  const submit = mountForm({ changes: { type: 'object' }, handles: { type: 'array' } })
  edit('Changes', '{'); edit('Handles', '['); edit('Changes', '{}'); blockedForm(); runForm()
  expect(screen.getAllByRole('alert')).toHaveLength(1); expect(submit).not.toHaveBeenCalled()
})

const STALE = (label) => `${label} has a value this tool no longer accepts. Choose or enter a new one.`
function expectFieldError(label, error) {
  const input = field(label)
  expect(input.getAttribute('aria-invalid')).toBe(error ? 'true' : 'false')
  expect(input.validationMessage).toBe(error)
  if (error) {
    const alert = screen.getByText(error, { selector: '[role="alert"]' })
    expect(input.getAttribute('aria-describedby').split(' ')).toContain(alert.id)
  }
}

function expectFormValidity(host, valid, errors) {
  expect(host.onValidityChange).toHaveBeenLastCalledWith(valid)
  expect(screen.queryAllByRole('alert').map((node) => node.textContent)).toEqual(errors)
}

it('FORM31 a nullable boolean default stays valid without a choice control', () => {
  const schema = { properties: { x: { type: ['boolean', 'null'], default: null } } }
  expect(defaultsOf(schema)).toEqual({ x: null })
  const host = swapHost(schema, defaultsOf(schema))
  expectFieldError('X', '')
  expectFormValidity(host, true, [])
  expect(field('X').tagName).toBe('INPUT')
  expect(field('X').type).toBe('text')
  expect(field('X').value).toBe('')
  expect(host.onChange).not.toHaveBeenCalled()
})

it('FORM34 boolean and string alternatives validate every committed type', () => {
  const schema = { properties: { x: { type: ['boolean', 'string'] } } }
  const host = swapHost(schema, { x: 'roof' })
  expectFieldError('X', '')
  expectFormValidity(host, true, [])
  expect(field('X').tagName).toBe('INPUT')
  expect(field('X').type).toBe('text')
  expect(field('X').value).toBe('roof')
  for (const x of [false, true]) {
    host.swap(schema, { x })
    expectFieldError('X', '')
    expectFormValidity(host, true, [])
  }
  host.swap(schema, { x: 5 })
  expectFieldError('X', STALE('X'))
  expectFormValidity(host, false, [STALE('X')])
})

it('FORM35 object enum matching ignores key order', () => {
  const host = swapHost({ properties: { x: { type: 'object', enum: [{ a: 1, b: 2 }] } } }, { x: { b: 2, a: 1 } })
  expectFieldError('X', '')
  expectFormValidity(host, true, [])
  expect(field('X').value).toBe('0')
  expect(field('X').selectedOptions[0].textContent).toBe('{"a":1,"b":2}')
  expect(host.onChange).not.toHaveBeenCalled()
})

it('FORM36 NaN cannot select a null enum member', () => {
  const schema = { properties: { x: { type: ['number', 'null'], enum: [null, 1] } } }
  const host = swapHost(schema, { x: NaN })
  expectFieldError('X', STALE('X'))
  expectFormValidity(host, false, [STALE('X')])
  expect(field('X').value).toBe('')
  expect(host.onChange).not.toHaveBeenCalled()
  host.swap(schema, { x: null })
  expectFieldError('X', '')
  expectFormValidity(host, true, [])
  expect(field('X').value).toBe('0')
})

it('FORM37 infinities cannot select a null enum member', () => {
  const schema = { properties: { x: { type: ['number', 'null'], enum: [null, 1] } } }
  const host = swapHost(schema, { x: Infinity })
  for (const x of [Infinity, -Infinity]) {
    host.swap(schema, { x })
    expectFieldError('X', STALE('X'))
    expectFormValidity(host, false, [STALE('X')])
    expect(field('X').value).toBe('')
  }
  host.swap(schema, { x: 1 })
  expectFieldError('X', '')
  expectFormValidity(host, true, [])
  expect(field('X').value).toBe('1')
})

it('FORM38 a nullable sibling stays valid through JSON schema changes and correction', () => {
  const x = { type: ['boolean', 'null'], default: null }
  const host = swapHost({ properties: { changes: { type: 'object' }, x } }, { changes: { tilt: 20 }, x: null })
  expectFormValidity(host, true, [])
  expectFieldError('Changes', '')
  expectFieldError('X', '')
  const schema = { properties: { changes: { type: 'array' }, x } }
  host.swap(schema, { changes: { tilt: 20 }, x: null })
  expectFormValidity(host, false, [STALE('Changes')])
  expect(field('Changes').value).toBe('{"tilt":20}')
  expectFieldError('Changes', STALE('Changes'))
  expectFieldError('X', '')
  edit('Changes', '{')
  expectFieldError('Changes', 'Changes must contain valid JSON.')
  expectFormValidity(host, false, ['Changes must contain valid JSON.'])
  expectFieldError('X', '')
  edit('Changes', 'false')
  expectFieldError('Changes', 'Changes must contain a JSON array.')
  expectFormValidity(host, false, ['Changes must contain a JSON array.'])
  expectFieldError('X', '')
  edit('Changes', '[1]')
  expect(host.onChange).toHaveBeenLastCalledWith({ changes: [1], x: null }, { changedKey: 'changes' })
  host.swap(schema, { changes: [1], x: null })
  expectFormValidity(host, true, [])
  expectFieldError('Changes', '')
  expectFieldError('X', '')
})

it('FORM39 a nullable sibling stays valid when an enum member is dropped and replaced', () => {
  const x = { type: ['boolean', 'null'], default: null }
  const host = swapHost({ properties: { phase: { type: 'integer', enum: [1, 3] }, x } }, { phase: 3, x: null })
  expectFormValidity(host, true, [])
  expectFieldError('Phase', '')
  expectFieldError('X', '')
  expect(field('Phase').value).toBe('1')
  const schema = { properties: { phase: { type: 'integer', enum: [1] }, x } }
  host.swap(schema, { phase: 3, x: null })
  expectFormValidity(host, false, [STALE('Phase')])
  expectFieldError('Phase', STALE('Phase'))
  expectFieldError('X', '')
  expect(field('Phase').value).toBe('')
  edit('Phase', '0')
  expect(host.onChange).toHaveBeenLastCalledWith({ phase: 1, x: null }, { changedKey: 'phase' })
  host.swap(schema, { phase: 1, x: null })
  expectFormValidity(host, true, [])
  expectFieldError('Phase', '')
  expectFieldError('X', '')
  expect(field('Phase').value).toBe('0')
})

it('FORM40 nullable and untyped siblings stay valid through scalar type swaps', () => {
  const x = { type: ['boolean', 'null'], default: null }, free = { description: 'Anything.' }
  const host = swapHost({ properties: { n: { type: 'integer' }, free, x } }, { n: 5, free: 5, x: null })
  expectFormValidity(host, true, [])
  for (const label of ['N', 'Free', 'X']) expectFieldError(label, '')
  host.swap({ properties: { n: { type: 'string' }, free, x } }, { n: 5, free: 5, x: null })
  expectFormValidity(host, false, [STALE('N')])
  expectFieldError('N', STALE('N'))
  expectFieldError('Free', '')
  expectFieldError('X', '')
  const schema = { properties: { n: { type: 'boolean' }, free, x } }
  host.swap(schema, { n: 'true', free: 5, x: null })
  expectFormValidity(host, false, [STALE('N')])
  expectFieldError('N', STALE('N'))
  expectFieldError('Free', '')
  expectFieldError('X', '')
  expect(field('N').value).toBe('')
  host.swap(schema, { n: true, free: 5, x: null })
  expectFormValidity(host, true, [])
  for (const label of ['N', 'Free', 'X']) expectFieldError(label, '')
  expect(field('N').value).toBe('1')
})

it('FORM41 a nullable sibling permits recovery from a strict number endpoint', () => {
  const submit = mountForm({ ratio: { type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 },
    x: { type: ['boolean', 'null'], default: null } })
  edit('Ratio', '1')
  blockedForm()
  expectFieldError('Ratio', 'Ratio is outside its allowed range.')
  expectFieldError('X', '')
  expect(screen.queryAllByRole('alert').map((node) => node.textContent)).toEqual(['Ratio is outside its allowed range.'])
  runForm()
  expect(submit).not.toHaveBeenCalled()
  edit('Ratio', '0.999')
  expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false)
  expectFieldError('Ratio', '')
  expectFieldError('X', '')
  expect(screen.queryAllByRole('alert')).toEqual([])
  runForm()
  expect(submit).toHaveBeenCalledExactlyOnceWith({ ratio: 0.999, x: null })
})

it('FORM42 a nullable sibling permits both strict integer endpoints', () => {
  for (const bounds of [{ minimum: 1, maximum: 5, exclusiveMinimum: true, exclusiveMaximum: true },
    { exclusiveMinimum: 1.2, exclusiveMaximum: 4.8 }]) {
    const submit = mountForm({ count: { type: 'integer', ...bounds }, x: { type: ['boolean', 'null'], default: null } })
    expect(field('Count').min).toBe('2')
    expect(field('Count').max).toBe('4')
    for (const value of ['1', '5']) {
      edit('Count', value)
      blockedForm()
      expectFieldError('Count', 'Count is outside its allowed range.')
      expectFieldError('X', '')
      expect(screen.queryAllByRole('alert').map((node) => node.textContent)).toEqual(['Count is outside its allowed range.'])
    }
    for (const value of ['2', '4']) {
      edit('Count', value)
      expect(screen.getByRole('button', { name: 'Run' }).disabled).toBe(false)
      expectFieldError('Count', '')
      expectFieldError('X', '')
      expect(screen.queryAllByRole('alert')).toEqual([])
      runForm()
    }
    expect(submit.mock.calls).toEqual([[{ count: 2, x: null }], [{ count: 4, x: null }]])
    cleanup()
  }
})

it('FORM45 array enum matching preserves order, primitive types and length', () => {
  const schema = { properties: { x: { type: 'array', enum: [[{ a: 1, b: 2 }, 2]] } } }
  const host = swapHost(schema, { x: [{ b: 2, a: 1 }, 2] })
  expectFormValidity(host, true, [])
  expectFieldError('X', '')
  expect(field('X').value).toBe('0')
  for (const x of [[2, { b: 2, a: 1 }], [{ b: 2, a: 1 }, '2'], [{ b: 2, a: 1 }]]) {
    host.swap(schema, { x })
    expectFormValidity(host, false, [STALE('X')])
    expectFieldError('X', STALE('X'))
    expect(field('X').value).toBe('')
  }
})

it('FORM46 nested non-finite numbers cannot match a null enum value', () => {
  const schema = { properties: { x: { type: 'object', enum: [{ a: null }] } } }
  const host = swapHost(schema, { x: { a: NaN } })
  for (const a of [NaN, Infinity, -Infinity]) {
    host.swap(schema, { x: { a } })
    expectFormValidity(host, false, [STALE('X')])
    expectFieldError('X', STALE('X'))
    expect(field('X').value).toBe('')
  }
  host.swap(schema, { x: { a: null } })
  expectFormValidity(host, true, [])
  expectFieldError('X', '')
  expect(field('X').value).toBe('0')
})

it('FORM47 enum equality allows depth 64 and rejects depth 65 including shared objects', () => {
  const nest = (depth) => depth === 0 ? 1 : { x: nest(depth - 1) }
  const host = swapHost({ properties: { x: { type: 'object', enum: [nest(64)] } } }, { x: nest(64) })
  expectFormValidity(host, true, [])
  expectFieldError('X', '')
  expect(field('X').value).toBe('0')
  host.swap({ properties: { x: { type: 'object', enum: [nest(65)] } } }, { x: nest(65) })
  expectFormValidity(host, false, [STALE('X')])
  expectFieldError('X', STALE('X'))
  expect(field('X').value).toBe('')
  const shared = nest(65)
  host.swap({ properties: { x: { type: 'object', enum: [shared] } } }, { x: shared })
  expectFormValidity(host, false, [STALE('X')])
  expectFieldError('X', STALE('X'))
  expect(field('X').value).toBe('')
})

it('FORM48 singleton boolean type lists keep the type matcher and text control', () => {
  const schema = { properties: { x: { type: ['boolean'] } } }
  const host = swapHost(schema, { x: true })
  expectFormValidity(host, true, [])
  expectFieldError('X', '')
  expect(field('X').tagName).toBe('INPUT')
  expect(field('X').type).toBe('text')
  expect(field('X').value).toBe('true')
  host.swap(schema, { x: false })
  expectFormValidity(host, true, [])
  expectFieldError('X', '')
  expect(field('X').value).toBe('false')
  host.swap(schema, { x: 'true' })
  expectFormValidity(host, false, [STALE('X')])
  expectFieldError('X', STALE('X'))
  expect(valueMatchesSchemaType(null, { type: ['integer', 'null'] })).toBe(true)
  expect(valueMatchesSchemaType(2, { type: ['integer', 'null'] })).toBe(true)
  expect(valueMatchesSchemaType(1.5, { type: ['integer', 'null'] })).toBe(false)
  for (const value of [NaN, Infinity]) expect(valueMatchesSchemaType(value, { type: ['number', 'integer', 'null'] })).toBe(false)
})

function swapHost(schema, values) {
  const onChange = vi.fn(), onValidityChange = vi.fn()
  const view = render(<SchemaForm schema={schema} values={values} onChange={onChange} onValidityChange={onValidityChange} />)
  const swap = (nextSchema, nextValues = values) => view.rerender(
    <SchemaForm schema={nextSchema} values={nextValues} onChange={onChange} onValidityChange={onValidityChange} />)
  return { onChange, onValidityChange, swap }
}

it('FORM24 a JSON schema swap keeps a retained value blocked until it is replaced', () => {
  const host = swapHost({ properties: { changes: { type: 'object' } } }, { changes: { tilt: 20 } })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(host.onValidityChange).toHaveBeenLastCalledWith(true)
  host.swap({ properties: { changes: { type: 'array' } } })
  expect(field('Changes').value).toBe('{"tilt":20}')
  expect(screen.getByRole('alert').textContent).toBe(STALE('Changes'))
  expect(field('Changes').getAttribute('aria-invalid')).toBe('true')
  expect(field('Changes').validationMessage).toBe(STALE('Changes'))
  expect(host.onValidityChange).toHaveBeenLastCalledWith(false)
  edit('Changes', '[1]')
  expect(host.onChange).toHaveBeenLastCalledWith({ changes: [1] }, { changedKey: 'changes' })
  host.swap({ properties: { changes: { type: 'array' } } }, { changes: [1] })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(host.onValidityChange).toHaveBeenLastCalledWith(true)
})

it('FORM25 an enum swap that drops the chosen member blocks the run', () => {
  const host = swapHost({ properties: { phase: { type: 'integer', enum: [1, 3] } } }, { phase: 3 })
  expect(field('Phase').value).toBe('1')
  expect(host.onValidityChange).toHaveBeenLastCalledWith(true)
  host.swap({ properties: { phase: { type: 'integer', enum: [1] } } })
  expect(field('Phase').value).toBe('')
  expect(screen.getByRole('alert').textContent).toBe(STALE('Phase'))
  expect(host.onValidityChange).toHaveBeenLastCalledWith(false)
  edit('Phase', '0')
  expect(host.onChange).toHaveBeenLastCalledWith({ phase: 1 }, { changedKey: 'phase' })
  host.swap({ properties: { phase: { type: 'integer', enum: [1] } } }, { phase: 1 })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(host.onValidityChange).toHaveBeenLastCalledWith(true)
})

it('FORM26 a scalar type swap blocks a retained value while an untyped field keeps any value', () => {
  const free = { description: 'Anything.' }
  const host = swapHost({ properties: { n: { type: 'integer' }, free } }, { n: 5, free: 5 })
  expect(screen.queryByRole('alert')).toBeNull()
  host.swap({ properties: { n: { type: 'string' }, free } })
  expect(screen.getAllByRole('alert').map((node) => node.textContent)).toEqual([STALE('N')])
  expect(host.onValidityChange).toHaveBeenLastCalledWith(false)
  host.swap({ properties: { n: { type: 'boolean' }, free } }, { n: 'true', free: 5 })
  expect(field('N').value).toBe('')
  expect(screen.getAllByRole('alert').map((node) => node.textContent)).toEqual([STALE('N')])
  host.swap({ properties: { n: { type: 'boolean' }, free } }, { n: true, free: 5 })
  expect(screen.queryByRole('alert')).toBeNull()
  expect(host.onValidityChange).toHaveBeenLastCalledWith(true)
})

it('FORM27 a strict number upper bound blocks its endpoint', () => {
  const submit = mountForm({ ratio: { type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 1 } })
  edit('Ratio', '1'); blockedForm(); runForm()
  expect(field('Ratio').validationMessage).toBe('Ratio is outside its allowed range.')
  expect(submit).not.toHaveBeenCalled()
  edit('Ratio', '0.999'); runForm()
  expect(submit).toHaveBeenCalledWith({ ratio: 0.999 })
})

it('FORM28 strict integer bounds submit both whole endpoints, legacy and fractional', () => {
  for (const bounds of [{ minimum: 1, maximum: 5, exclusiveMinimum: true, exclusiveMaximum: true },
    { exclusiveMinimum: 1.2, exclusiveMaximum: 4.8 }]) {
    const submit = mountForm({ count: { type: 'integer', ...bounds } })
    expect(field('Count').min).toBe('2'); expect(field('Count').max).toBe('4')
    for (const value of ['1', '5']) { edit('Count', value); blockedForm() }
    edit('Count', '2'); runForm(); expect(submit).toHaveBeenLastCalledWith({ count: 2 })
    edit('Count', '4'); runForm(); expect(submit).toHaveBeenLastCalledWith({ count: 4 })
    cleanup()
  }
})
