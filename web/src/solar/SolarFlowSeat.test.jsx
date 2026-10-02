import React from 'react'
import { cleanup, render } from '@testing-library/react'
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

it('SEAT U2 seats both children in order with nothing outside', () => {
  const { container } = render(<SolarFlowSeat seated={true}>{children}</SolarFlowSeat>)
  expect(container.childNodes).toHaveLength(1)
  const seats = container.querySelectorAll('div.solar-flow-seat[data-testid="solar-flow-seat"]')
  expect(seats).toHaveLength(1)
  expect(seats[0].children).toHaveLength(2)
  expect([...seats[0].children].map((child) => child.textContent)).toEqual(['First', 'Second'])
})
