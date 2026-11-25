import { expect, it } from 'vitest'
import { headerChromeFor } from '../App.jsx'

it.each([
  ['cad', { grant: { linked: true } }, false, true],
  ['cad', { grant: { linked: false } }, false, true],
  ['cad', { grant: null }, false, true],
  ['cad', { grant: { linked: true }, signedOut: true }, false, true],
  ['cad', { grant: { linked: true }, grantLoading: true }, false, true],
  ['cad', { grant: { linked: true }, grantErr: 'unavailable' }, false, true],
  ['solar', { grant: { linked: true } }, true, true],
])('shows the expected header controls for %s with %o', (surface, state, cost, accounts) => {
  expect(headerChromeFor({ studioGround: true, surface, ...state })).toEqual({ cost, accounts })
})
