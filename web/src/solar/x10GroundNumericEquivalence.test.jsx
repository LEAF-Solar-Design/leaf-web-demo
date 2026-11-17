import { cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarGroundInputForm from './SolarGroundInputForm.jsx'

afterEach(cleanup)

function mount() {
  const captures = []
  const callback = vi.fn(value => captures.push(value === null ? null : JSON.parse(JSON.stringify(value))))
  const view = render(<SolarGroundInputForm onParameters={callback} />)
  const scope = within(view.container.querySelector('fieldset'))
  const edit = (label, value) => fireEvent.change(scope.getByLabelText(label), { target: { value } })
  edit('Site width (drawing units)', '10')
  edit('Site height (drawing units)', '20')
  return { view, scope, edit, captures, callback, current: () => captures.at(-1) }
}

describe('ground numeric semantic snapshots', () => {
  it.each([
    ['m', '300000000', '0.000001', true],
    ['ft', '999999999', '0.000001', true],
    ['ft', '0', '10', false],
    ['m', '0', '3.048', false],
  ])('compares actual source and converted %s corner readouts at origin %s', (units, origin, size, warns) => {
    const { edit, scope, current } = mount()
    const read = name => within(scope.getByRole('table', { name })).getAllByRole('row').slice(1)
      .map(row => within(row).getAllByRole('cell').map(cell => Number(cell.textContent)))
    let observedLoss = false
    // Exercise represented edges at several adjacent scales; compare actual
    // readouts rather than assuming a decimal token's rounding bias.
    for (const edge of warns ? ['0.0000001', '0.0000002', '0.0000003', '0.0000004', '0.0000005', size, '0.000002', '0.000003'] : [size]) {
      edit('Site drawing units', units)
      edit('Site origin X (drawing units)', origin)
      edit('Site origin Y (drawing units)', origin)
      edit('Site width (drawing units)', edge)
      edit('Site height (drawing units)', edge)
      const source = current()
      expect(read(`Site boundary coordinates (drawing units: ${units})`)).toEqual(source.boundary)
      fireEvent.click(scope.getByRole('button', { name: /Convert site units to/ }))
      const target = current(), factor = u => u === 'ft' ? 0.3048 : 1
      expect(target.drawing_units).toBe(units === 'm' ? 'ft' : 'm')
      const before = read(`Source boundary before conversion (drawing units: ${units})`)
      const after = read(`Site boundary coordinates (drawing units: ${target.drawing_units})`)
      expect(before).toEqual(source.boundary)
      expect(after).toEqual(target.boundary)
      const changed = before.some((point, i) => point.some((value, axis) =>
        value * factor(units) !== after[i][axis] * factor(target.drawing_units)))
      const warning = scope.getByRole('status').textContent.includes('Precision loss: unit conversion changed represented corners. Review the boundary coordinates.')
      fireEvent.click(scope.getByRole('button', { name: /Convert site units to/ }))
      const returned = current()
      const roundtripChanged = source.boundary.some((point, i) => point.some((value, axis) =>
        value * factor(units) !== returned.boundary[i][axis] * factor(units)))
      expect(warning).toBe(changed || roundtripChanged)
      observedLoss ||= changed || roundtripChanged
      expect(read(`Site boundary coordinates (drawing units: ${units})`)).toEqual(returned.boundary)
    }
    expect(observedLoss).toBe(warns)
    edit('Site origin X (drawing units)', '-1')
    expect(scope.queryByRole('status')).toBeNull()
    expect(scope.queryByRole('table', { name: /Source boundary before conversion/ })).toBeNull()
  })

  it('reviews converted corners derived from valid site values when module validation prevents a payload', () => {
    const { edit, scope, current } = mount()
    edit('Module power (watts)', 'bad')
    expect(current()).toBeNull()
    fireEvent.click(scope.getByRole('button', { name: 'Convert site units to feet' }))
    expect(scope.getByRole('status').textContent).toContain('Site units converted.')
    const tablePoints = name => within(scope.getByRole('table', { name })).getAllByRole('row').slice(1)
      .map(row => within(row).getAllByRole('cell').map(cell => Number(cell.textContent)))
    expect(tablePoints('Source boundary before conversion (drawing units: m)')).toEqual([[0, 0], [10, 0], [10, 20], [0, 20]])
    const x = Number(scope.getByLabelText('Site width (drawing units)').value)
    const y = Number(scope.getByLabelText('Site height (drawing units)').value)
    expect(tablePoints('Converted boundary after conversion (drawing units: ft)')).toEqual([[0, 0], [x, 0], [x, y], [0, y]])
    expect(current()).toBeNull()
    expect(scope.queryByLabelText('Generated source input parameters JSON')).toBeNull()
  })

  it.each(['中文 🌞', 'Default'])('defers all IME snapshots until completed or cancelled name %s', name => {
    const { scope, edit, captures, current } = mount()
    const original = current(), count = captures.length
    const input = scope.getByLabelText('Preset name')
    fireEvent.compositionStart(input)
    expect(current()).toBeNull()
    expect(captures).toHaveLength(count + 1)
    expect(scope.queryByLabelText('Generated source input parameters JSON')).toBeNull()
    expect(scope.queryByRole('button', { name: 'Copy generated JSON' })).toBeNull()
    for (const value of ['中', '中文', '中文 🌞']) edit('Preset name', value)
    expect(captures).toHaveLength(count + 1)
    fireEvent.compositionEnd(input, { target: { value: name } })
    expect(captures).toHaveLength(count + 2)
    expect(current()).toEqual({ ...original, preset: { ...original.preset, Name: name } })
    edit('Preset name', name)
    expect(captures).toHaveLength(count + 2)
  })

  it('reset cancels composition and ignores late completion; completed invalid Unicode is refused', () => {
    const { scope, edit, captures, current } = mount()
    const input = scope.getByLabelText('Preset name')
    fireEvent.compositionStart(input)
    edit('Preset name', '中')
    fireEvent.click(scope.getByRole('button', { name: 'Reset structured inputs' }))
    const count = captures.length
    fireEvent.compositionEnd(input, { data: 'late 🌞' })
    expect(current()).toBeNull()
    expect(captures).toHaveLength(count)
    expect(input.value).toBe('Default')
    expect(scope.queryByRole('alert')).toBeNull()
    edit('Site width (drawing units)', '10'); edit('Site height (drawing units)', '20')
    fireEvent.compositionStart(input)
    fireEvent.compositionEnd(input, { target: { value: '\ud800' } })
    expect(current()).toBeNull()
    expect(scope.getByRole('alert').textContent).toContain('unpaired Unicode')
  })

  it('reports exact per-frame count capacity independently of unrelated invalid fields', () => {
    const { edit, scope, current } = mount()
    expect(scope.getByText('Remaining per-frame modules: 9952 (10,000 module limit).')).toBeTruthy()
    edit('Module rows', '1000'); edit('Module columns', '10')
    expect(current()).not.toBeNull()
    expect(scope.getByText('Remaining per-frame modules: 0 (10,000 module limit).')).toBeTruthy()
    edit('Module rows', '73'); edit('Module columns', '137')
    expect(current()).toBeNull()
    expect(scope.getByText('Per-frame module count exceeds the 10,000 module limit by 1.')).toBeTruthy()
    edit('Module power (watts)', 'bad')
    expect(scope.getByText(/exceeds the 10,000 module limit by 1/)).toBeTruthy()
    for (const value of ['4.5', '', '1001', '1.00000000000000001']) {
      edit('Module rows', value)
      expect(scope.queryByText(/Remaining per-frame modules:|Per-frame module count exceeds/)).toBeNull()
      expect(current()).toBeNull()
    }
  })

  it.each([
    ['Site width (drawing units)', '10', ' +001.000e1 ', '10e'],
    ['Site height (drawing units)', '20', '00200e-1', '20x'],
    ['Site origin X (drawing units)', '-2', ' -00020e-1 ', '--2'],
    ['Site origin Y (drawing units)', '0', '-0.00e-999', '1e-999'],
    ['Module length (metres)', '2.5', '025e-1', '0x2'],
    ['Module width (metres)', '1.25', '+.125e1', '1.2.5'],
    ['Module thickness (metres)', '0.04', '4e-2', 'Infinity'],
    ['Module power (watts)', '100', '10000e-2', '100.00000000000000001'],
    ['Module rows', '4', '0400e-2', '4.00000000000000001'],
    ['Module columns', '12', '+12.00e0', '11.99999999999999999'],
    ['Horizontal module gap (metres)', '0', '0e-999', '-1e-999'],
    ['Vertical module gap (metres)', '0.02', ' 0002e-2 ', '.'],
    ['Tilt (degrees)', '0', '-0', 'NaN'],
    ['Axis azimuth (degrees)', '180', '0018e1', '1e309'],
    ['Preset name', 'Default', '  Default  ', ''],
  ])('suppresses equivalent %s tokens and emits null only once for malformed neighbors', (label, first, equivalent, invalid) => {
    const { edit, scope, captures, current } = mount()
    edit(label, first)
    const expected = current()
    expect(expected).not.toBeNull()
    const count = captures.length
    edit(label, equivalent)
    expect(scope.getByLabelText(label).value).toBe(equivalent)
    expect(JSON.parse(scope.getByLabelText('Generated source input parameters JSON').textContent)).toEqual(expected)
    expect(captures).toHaveLength(count)
    edit(label, invalid)
    expect(current()).toBeNull()
    expect(captures).toHaveLength(count + 1)
    edit(label, label === 'Preset name' ? '   ' : 'bad')
    expect(captures).toHaveLength(count + 1)
    edit(label, first)
    expect(current()).toEqual(expected)
    expect(captures).toHaveLength(count + 2)
  })

  it.each([
    ['Site origin X (drawing units)', '-1e9', '-1000000001'],
    ['Site origin Y (drawing units)', '-1e9', '-1000000001'],
    ['Site width (drawing units)', '1e6', '1000001'],
    ['Site height (drawing units)', '1e6', '1000001'],
    ['Module length (metres)', '1e2', '100.1'],
    ['Module width (metres)', '1e2', '100.1'],
    ['Module thickness (metres)', '1e0', '1.1'],
    ['Module power (watts)', '1e6', '1000001'],
    ['Horizontal module gap (metres)', '1e2', '100.1'],
    ['Vertical module gap (metres)', '1e2', '100.1'],
    ['Module rows', '1e3', '1001'],
    ['Module columns', '1e3', '1001'],
    ['Tilt (degrees)', '9e1', '90.1'],
    ['Axis azimuth (degrees)', '36e1', '360.1'],
  ])('captures the finite %s bound and refuses its neighbor', (label, valid, invalid) => {
    const { edit, captures, current } = mount()
    edit('Module rows', '1'); edit('Module columns', '1')
    edit(label, valid)
    expect(current()).not.toBeNull()
    const expected = current(), count = captures.length
    edit(label, String(Number(valid)))
    expect(captures).toHaveLength(count)
    edit(label, invalid)
    expect(current()).toBeNull()
    edit(label, valid)
    expect(current()).toEqual(expected)
    expect(captures).toHaveLength(count + 2)
  })

  it('delivers current independent snapshots to replacement callbacks through valid and null transitions', () => {
    const { view, edit, captures, callback, current } = mount()
    const expected = current(), count = captures.length
    const replacement = vi.fn()
    view.rerender(<SolarGroundInputForm onParameters={replacement} />)
    expect(replacement).toHaveBeenCalledExactlyOnceWith(expected)
    expect(replacement.mock.calls[0][0]).not.toBe(callback.mock.calls.at(-1)[0])
    edit('Site width (drawing units)', '1e1')
    expect(replacement).toHaveBeenCalledTimes(1)
    edit('Site width (drawing units)', 'bad')
    edit('Site width (drawing units)', 'worse')
    expect(replacement).toHaveBeenCalledTimes(2)
    expect(replacement.mock.calls.at(-1)[0]).toBeNull()
    const invalidReplacement = vi.fn()
    view.rerender(<SolarGroundInputForm onParameters={invalidReplacement} />)
    expect(invalidReplacement).toHaveBeenCalledExactlyOnceWith(null)
    edit('Site width (drawing units)', '10.0')
    expect(invalidReplacement).toHaveBeenCalledTimes(2)
    expect(invalidReplacement.mock.calls.at(-1)[0]).toEqual(expected)
    expect(captures).toHaveLength(count)
  })

  it('emits reinterpretation despite identical tokens and emits conversion without feedback duplicates', () => {
    const { edit, scope, captures, current } = mount()
    const metres = current(), count = captures.length
    edit('Site drawing units', 'ft')
    expect(current()).toEqual({ ...metres, drawing_units: 'ft' })
    expect(captures).toHaveLength(count + 1)
    expect(scope.getByLabelText('Site width (drawing units)').value).toBe('10')
    fireEvent.click(scope.getByRole('button', { name: 'Convert site units to metres' }))
    expect(current()).toMatchObject({ drawing_units: 'm', preset: metres.preset,
      boundary: [[0, 0], [3.048, 0], [3.048, 6.096], [0, 6.096]] })
    expect(captures).toHaveLength(count + 2)
    expect(scope.getByRole('status').textContent).toContain('Site units converted.')
    edit('Site width (drawing units)', '3.048e0')
    expect(scope.queryByRole('status')).toBeNull()
    expect(captures).toHaveLength(count + 2)
  })
})
