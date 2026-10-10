import { expect, it } from 'vitest'
import { isCompositionKey } from './keyComposition.js'

it('KEYS-D01 null and undefined are not composition keys', () => {
  expect(isCompositionKey(null)).toBe(false)
  expect(isCompositionKey(undefined)).toBe(false)
})

it('KEYS-D02 numeric inputs are not composition events', () => {
  for (const event of [0, 229, 229.0, NaN, Infinity]) expect(isCompositionKey(event)).toBe(false)
})

it('KEYS-D03 string inputs are not composition events', () => {
  for (const event of ['', '229', 'Enter']) expect(isCompositionKey(event)).toBe(false)
})

it('KEYS-D04 absent and primitive native events are safe', () => {
  for (const event of [{}, Object.create(null), ...[undefined, null, 229, 'Enter'].map((nativeEvent) => ({ nativeEvent }))]) {
    expect(isCompositionKey(event)).toBe(false)
  }
})

it('KEYS-D05 a direct true composition flag is recognized', () => {
  expect(isCompositionKey({ isComposing: true })).toBe(true)
  expect(isCompositionKey({ isComposing: true, keyCode: 0, nativeEvent: { isComposing: false, keyCode: 0 } })).toBe(true)
})

it('KEYS-D06 a native true composition flag is recognized', () => {
  expect(isCompositionKey({ nativeEvent: { isComposing: true } })).toBe(true)
  expect(isCompositionKey({ isComposing: false, nativeEvent: { isComposing: true } })).toBe(true)
})

it('KEYS-D07 direct numeric 229 is recognized', () => {
  for (const keyCode of [229, 229.0]) expect(isCompositionKey({ isComposing: false, keyCode })).toBe(true)
})

it('KEYS-D08 native numeric 229 is recognized', () => {
  for (const keyCode of [229, 229.0]) {
    expect(isCompositionKey({ isComposing: false, keyCode: 0, nativeEvent: { isComposing: false, keyCode } })).toBe(true)
  }
})

it('KEYS-D09 composition marks use strict comparisons', () => {
  for (const isComposing of [false, 1, 'true']) {
    expect(isCompositionKey({ isComposing })).toBe(false)
    expect(isCompositionKey({ nativeEvent: { isComposing } })).toBe(false)
  }
  for (const keyCode of ['229', true, false, 0, 228, 230]) {
    expect(isCompositionKey({ keyCode })).toBe(false)
    expect(isCompositionKey({ nativeEvent: { keyCode } })).toBe(false)
  }
})

it('KEYS-D10 any valid mark wins without changing the event', () => {
  const fixtures = [
    { isComposing: true, keyCode: '229', nativeEvent: { isComposing: false, keyCode: 0 } },
    { isComposing: 1, keyCode: 0, nativeEvent: { isComposing: true, keyCode: '229' } },
    { isComposing: false, keyCode: 229, nativeEvent: { isComposing: 'true', keyCode: 0 } },
    { isComposing: 'true', keyCode: true, nativeEvent: { isComposing: false, keyCode: 229 } },
  ]
  for (const fixture of fixtures) {
    const snapshot = { ...fixture, nativeEvent: { ...fixture.nativeEvent } }
    Object.freeze(fixture.nativeEvent)
    Object.freeze(fixture)
    expect(isCompositionKey(fixture)).toBe(true)
    expect(fixture).toEqual(snapshot)
    expect(Object.isFrozen(fixture)).toBe(true)
    expect(Object.isFrozen(fixture.nativeEvent)).toBe(true)
  }
})
