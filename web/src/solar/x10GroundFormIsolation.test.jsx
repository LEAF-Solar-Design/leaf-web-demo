import { act, cleanup, fireEvent, render, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import SolarGroundInputForm from './SolarGroundInputForm.jsx'

afterEach(cleanup)

describe('ground form instance isolation', () => {
  it.each([0, 1])('keeps instance %s edits, feedback, reset and callbacks independent', async active => {
    const callbacks = [vi.fn(), vi.fn()]
    const forms = () => <>{callbacks.map((callback, index) =>
      <SolarGroundInputForm key={index} onParameters={callback} />)}</>
    const view = render(forms())
    const fieldsets = [...view.container.querySelectorAll('fieldset')]
    const scopes = fieldsets.map(fieldset => within(fieldset))
    const edit = (index, label, value) => fireEvent.change(scopes[index].getByLabelText(label), { target: { value } })
    const current = index => callbacks[index].mock.calls.at(-1)[0]
    const controls = index => [...fieldsets[index].querySelectorAll('input, select')].map(node => [node.id, node.value])
    const idsResolveLocally = () => {
      const ids = [...view.container.querySelectorAll('[id]')].map(node => node.id)
      expect(new Set(ids).size).toBe(ids.length)
      for (const fieldset of fieldsets) {
        for (const node of fieldset.querySelectorAll('[for], [aria-describedby], [aria-labelledby], a[href]')) {
          for (const attr of ['for', 'aria-describedby', 'aria-labelledby', 'href']) {
            const value = node.getAttribute(attr)
            if (!value) continue
            for (const id of value.replace(/^#/, '').split(' '))
              expect(fieldset.contains(document.getElementById(id))).toBe(true)
          }
        }
      }
    }
    for (const scope of scopes) expect(scope.queryByRole('alert')).toBeNull()
    expect(callbacks.map(callback => callback.mock.calls.at(-1)[0])).toEqual([null, null])
    for (const index of [0, 1]) {
      edit(index, 'Site width (drawing units)', index === 0 ? '10' : '30')
      edit(index, 'Site height (drawing units)', index === 0 ? '20' : '40')
      edit(index, 'Preset name', `Frame ${index}`)
    }
    idsResolveLocally()
    const other = 1 - active
    const untouched = { controls: controls(other), parameters: current(other),
      calls: callbacks[other].mock.calls.length, html: fieldsets[other].innerHTML }
    const assertOther = () => {
      expect(controls(other)).toEqual(untouched.controls)
      expect(current(other)).toBe(untouched.parameters)
      expect(callbacks[other].mock.calls).toHaveLength(untouched.calls)
      expect(fieldsets[other].innerHTML).toBe(untouched.html)
      expect(scopes[other].queryByRole('alert')).toBeNull()
      expect(scopes[other].queryByRole('status')).toBeNull()
    }
    edit(active, 'Site drawing units', 'ft')
    edit(active, 'Site origin X (drawing units)', '-5')
    edit(active, 'Module power (watts)', '550')
    expect(current(active)).toMatchObject({ drawing_units: 'ft', preset: { ModulePowerWp: 550 } })
    expect(current(active).boundary[0]).toEqual([-5, 0])
    assertOther()
    fireEvent.click(scopes[active].getByRole('button', { name: 'Convert site units to metres' }))
    expect(scopes[active].getByRole('status').textContent).toContain('Site units converted.')
    expect(current(active).boundary[0]).toEqual([-5 * 0.3048, 0])
    expect(current(active).drawing_units).toBe('m')
    assertOther()
    edit(active, 'Module rows', '1.00000000000000001')
    edit(active, 'Module power (watts)', '0')
    expect(current(active)).toBeNull()
    expect(scopes[active].getByRole('status')).toBeTruthy()
    expect(scopes[active].getAllByRole('alert')).toHaveLength(1)
    idsResolveLocally()
    fireEvent.click(scopes[active].getByRole('link', { name: 'Module rows' }))
    expect(document.activeElement).toBe(scopes[active].getByLabelText('Module rows'))
    edit(active, 'Module rows', '4')
    expect(scopes[active].queryByRole('link', { name: 'Module rows' })).toBeNull()
    expect(scopes[active].getByRole('link', { name: 'Module power (watts)' })).toBeTruthy()
    assertOther()
    edit(active, 'Site width (drawing units)', 'bad')
    expect(scopes[active].queryByRole('status')).toBeNull()
    fireEvent.click(scopes[active].getByRole('button', { name: 'Convert site units to feet' }))
    expect(scopes[active].getByRole('status').textContent).toContain('not converted')
    assertOther()
    edit(active, 'Site drawing units', 'ft')
    expect(scopes[active].queryByRole('status')).toBeNull()
    fireEvent.click(scopes[active].getByRole('button', { name: 'Reset structured inputs' }))
    expect(scopes[active].getByLabelText('Site width (drawing units)').value).toBe('')
    expect(scopes[active].getByLabelText('Site origin X (drawing units)').value).toBe('0')
    expect(scopes[active].getByLabelText('Module power (watts)').value).toBe('715')
    expect(scopes[active].getByLabelText('Site drawing units').value).toBe('m')
    expect(scopes[active].queryByRole('alert')).toBeNull()
    expect(scopes[active].queryByRole('status')).toBeNull()
    expect(current(active)).toBeNull()
    assertOther()
    edit(active, 'Site width (drawing units)', '50')
    edit(active, 'Site height (drawing units)', '60')
    const capture = current(active)
    const retired = callbacks[active]
    const retiredCalls = retired.mock.calls.length
    callbacks[active] = vi.fn()
    view.rerender(forms())
    expect(callbacks[active]).toHaveBeenCalledExactlyOnceWith(capture)
    await act(async () => { await Promise.resolve() })
    expect(retired.mock.calls).toHaveLength(retiredCalls)
    expect(current(active)).toBe(capture)
    assertOther()
    idsResolveLocally()
    view.unmount()
    const counts = callbacks.map(callback => callback.mock.calls.length)
    await act(async () => { await Promise.resolve() })
    expect(callbacks.map(callback => callback.mock.calls.length)).toEqual(counts)
  })
})
