import { describe, expect, it, vi } from 'vitest'
import { INVESTOR_LINK_CAPTURE_SOURCE, INVESTOR_LINK_GLOBAL, takeInvestorLink } from './investorLinkCapture.js'

const capture = new Function('window', 'location', 'history', INVESTOR_LINK_CAPTURE_SOURCE)
const token = 'v1.1234abcd.1800000000.1800000050.' + 'a'.repeat(43)

function run(hash, replaceState = vi.fn()) {
  const win = new EventTarget()
  const location = { pathname: '/app', search: '?keep=1', hash, reload: vi.fn() }
  const history = { state: { visit: 3 }, replaceState }
  capture(win, location, history)
  return { win, history, location }
}

describe('pre-bundle investor link capture', () => {
  it.each([[token, token], ['bad', ''], [token + '&extra', ''], ['', '']])('strips and captures an investor fragment', (input, value) => {
    const { win, history } = run('#investor=' + input)
    expect(history.replaceState).toHaveBeenCalledExactlyOnceWith(history.state, '', '/app?keep=1')
    expect(Object.getOwnPropertyDescriptor(win, INVESTOR_LINK_GLOBAL)).toEqual({
      value, enumerable: false, configurable: true, writable: false,
    })
    expect(takeInvestorLink(win)).toBe(value)
    expect(takeInvestorLink(win)).toBeUndefined()
    expect(Object.hasOwn(win, INVESTOR_LINK_GLOBAL)).toBe(false)
  })

  it.each(['#pricing', ''])('leaves other fragments alone', hash => {
    const { win, history } = run(hash)
    expect(history.replaceState).not.toHaveBeenCalled()
    expect(Object.hasOwn(win, INVESTOR_LINK_GLOBAL)).toBe(false)
  })

  it('swallows a history failure and publishes nothing', () => {
    const { win } = run('#investor=' + token, () => { throw new Error('blocked') })
    expect(takeInvestorLink(win)).toBeUndefined()
  })

  it('reloads only when a same-document navigation opens an investor link', () => {
    const { win, location } = run('')
    location.hash = '#pricing'
    win.dispatchEvent(new Event('hashchange'))
    expect(location.reload).not.toHaveBeenCalled()
    location.hash = '#investor=' + token
    win.dispatchEvent(new Event('hashchange'))
    expect(location.reload).toHaveBeenCalledOnce()
  })
})
