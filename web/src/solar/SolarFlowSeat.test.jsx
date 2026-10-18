import React from 'react'
import { cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, expect, it } from 'vitest'
import SolarFlowSeat from './SolarFlowSeat.jsx'

afterEach(cleanup)
const children = <><span>First</span><button type="button">Second</button></>

it('SEAT U1 returns two children untouched when unseated', () => {
  const plain = render(children)
  const { container } = render(<SolarFlowSeat seated={false}>{children}</SolarFlowSeat>)
  expect(container.children).toHaveLength(2)
  expect(container.querySelector('.solar-flow-seat')).toBeNull()
  expect(container.innerHTML).toBe(plain.container.innerHTML)
})

it('SEAT U2 discloses mounted children in order with an accessible toggle', () => {
  const { container, getByRole, getByTestId } = render(<SolarFlowSeat seated={true}>{children}</SolarFlowSeat>)
  expect(container.childNodes).toHaveLength(1)
  const seats = container.querySelectorAll('div.solar-flow-seat[data-testid="solar-flow-seat"]')
  expect(seats).toHaveLength(1)
  const content = getByTestId('solar-flow-seat-content')
  expect([...content.children].map((child) => child.textContent)).toEqual(['First', 'Second'])
  const toggle = getByRole('button', { name: 'Solar flow tools' })
  expect(toggle.getAttribute('aria-controls')).toBe(content.id)
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(seats[0].getAttribute('data-expanded')).toBe('false')
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('true')
  expect(seats[0].getAttribute('data-expanded')).toBe('true')
  fireEvent.click(toggle)
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(getByTestId('solar-flow-seat-content')).toBe(content)
})

it('SEAT U3 folding preserves edited input and child identity', () => {
  const { getByRole, getByLabelText, rerender } = render(<SolarFlowSeat seated><input aria-label="Draft name" defaultValue="Original" /></SolarFlowSeat>)
  const toggle = getByRole('button', { name: 'Solar flow tools' })
  const input = getByLabelText('Draft name')
  fireEvent.click(toggle)
  fireEvent.change(input, { target: { value: 'Retained' } })
  fireEvent.click(toggle)
  rerender(<SolarFlowSeat seated><input aria-label="Draft name" defaultValue="Original" /></SolarFlowSeat>)
  fireEvent.click(toggle)
  expect(getByLabelText('Draft name')).toBe(input)
  expect(input.value).toBe('Retained')
})
