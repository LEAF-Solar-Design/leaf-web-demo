import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ProjectBoardGround } from './ProjectBoardGround.jsx'
import SurfaceGrounds from './SurfaceGrounds.jsx'
import { BOARD_THEME_KEY } from '../lib/themePreference.js'

beforeEach(() => localStorage.removeItem(BOARD_THEME_KEY))
afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  localStorage.removeItem(BOARD_THEME_KEY)
})

const mount = (props = {}) => render(<ProjectBoardGround active worldSpace={false} {...props} />)
const board = () => screen.getByRole('region', { name: 'Project workspace' })
const toggle = () => screen.getByRole('button', { name: 'Light board' })
function expectDark() {
  expect(board().hasAttribute('data-board-theme')).toBe(false)
  expect(board().classList.contains('leaf-light')).toBe(false)
}
function expectLight() {
  expect(board().getAttribute('data-board-theme')).toBe('light')
  expect(board().classList.contains('leaf-light')).toBe(true)
  expect(toggle().getAttribute('aria-pressed')).toBe('true')
}

it('SSD1-24E board starts dark', () => {
  mount()
  expectDark()
  expect(toggle().getAttribute('aria-pressed')).toBe('false')
})
it('SSD1-24E board switches to light and stores the choice', () => {
  mount()
  fireEvent.click(toggle())
  expectLight()
  expect(localStorage.getItem(BOARD_THEME_KEY)).toBe('light')
})
it('SSD1-24E board remembers light after remount', () => {
  const view = mount()
  fireEvent.click(toggle())
  view.unmount()
  mount()
  expectLight()
})
it('SSD1-24E board switches back to dark', () => {
  mount()
  fireEvent.click(toggle())
  fireEvent.click(toggle())
  expectDark()
  expect(toggle().getAttribute('aria-pressed')).toBe('false')
  expect(localStorage.getItem(BOARD_THEME_KEY)).toBe('dark')
})
it('SSD1-24E board ignores a bogus stored choice', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'bogus')
  mount()
  expectDark()
})
it('SSD1-24E board still switches when storage writes throw', () => {
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Unavailable') })
  mount()
  fireEvent.click(toggle())
  expectLight()
})
it('SSD1-24E board keeps a Start board over a drawing dark', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  mount({ contained: true, themeable: false })
  expectDark()
  expect(screen.queryByRole('button', { name: 'Light board' })).toBeNull()
})

it('SSD1-24E board row 8: contained Browser board offers light in its header', () => {
  mount({ contained: true, themeable: true, onReturnToDrawing: vi.fn() })
  expectDark()
  const header = board().querySelector('.ground-board-header')
  expect(header.lastElementChild).toBe(toggle())
  expect(header.firstElementChild.querySelector('h1')).not.toBeNull()
  fireEvent.click(toggle())
  expectLight()
  expect(localStorage.getItem(BOARD_THEME_KEY)).toBe('light')
})

it('SSD1-24E board row 10', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  const view = render(<ProjectBoardGround active worldSpace={false} themeable={false} />)
  expectDark()
  expect(screen.queryByRole('button', { name: 'Light board' })).toBeNull()
  view.rerender(<ProjectBoardGround active worldSpace={false} themeable />)
  expectLight()
})

it('SSD1-24E board row 11', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  const view = render(<SurfaceGrounds surface="cad" boardVisible studioShell studioPresentation />)
  expectDark()
  expect(screen.queryByRole('button', { name: 'Light board' })).toBeNull()
  view.rerender(<SurfaceGrounds surface="browser" studioShell studioPresentation />)
  expectLight()
})

it('SSD1-24E board row 12', () => {
  const view = mount({ themeable: false })
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: BOARD_THEME_KEY, newValue: 'light' }))
  })
  view.rerender(<ProjectBoardGround active worldSpace={false} themeable />)
  expectLight()
})

it('SSD1-24E board row 13', () => {
  const view = mount({ themeable: false })
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.somethingElse', newValue: 'light' }))
  })
  view.rerender(<ProjectBoardGround active worldSpace={false} themeable />)
  expectDark()
  expect(toggle().getAttribute('aria-pressed')).toBe('false')
})

it('SSD1-24E board row 14', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  mount()
  expectLight()
  localStorage.removeItem(BOARD_THEME_KEY)
  act(() => {
    window.dispatchEvent(new StorageEvent('storage', { key: null }))
  })
  expectDark()
})

it('SSD1-24E board row 15', () => {
  const removeListener = vi.spyOn(window, 'removeEventListener')
  const view = mount()
  view.unmount()
  expect(removeListener).toHaveBeenCalledWith('storage', expect.any(Function))
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  expect(() => {
    act(() => {
      window.dispatchEvent(new StorageEvent('storage', { key: BOARD_THEME_KEY, newValue: 'light' }))
    })
  }).not.toThrow()
})

it('SSD1-24E board row 9: SurfaceGrounds themes Browser but not Start over CAD', () => {
  localStorage.setItem(BOARD_THEME_KEY, 'light')
  const view = render(<SurfaceGrounds surface="browser" studioShell studioPresentation />)
  expect(board().getAttribute('data-board-layout')).toBe('contained')
  expectLight()
  view.unmount()
  render(<SurfaceGrounds surface="cad" boardVisible studioShell studioPresentation />)
  expect(board().getAttribute('data-board-layout')).toBe('contained')
  expectDark()
  expect(screen.queryByRole('button', { name: 'Light board' })).toBeNull()
  expect(localStorage.getItem(BOARD_THEME_KEY)).toBe('light')
})
