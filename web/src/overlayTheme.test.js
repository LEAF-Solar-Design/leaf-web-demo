/**
 * T1 preview render. Each test names the failure it prevents; the sink test is
 * the one an adversarial review found in the card's equivalent code.
 */
import { afterEach, describe, expect, it } from 'vitest'

import {
  CSS_VAR_BY_TOKEN, applyOverlay, copyFor, cssVarsFor, isRenderableColor,
} from './overlayTheme.js'

afterEach(() => {
  document.documentElement.removeAttribute('style')
})

describe('what reaches a style sink', () => {
  it('refuses url() so the browser never issues the request', () => {
    // The review's finding, in the other sink: a chat request containing
    // url(...) made the viewer's browser fetch the attacker's URL.
    const vars = cssVarsFor({ 'color.canvas.bg': 'url(https://attacker.example/x)' })
    expect(vars).toEqual({})
  })

  it('refuses named colours, rgb(), and var() indirection', () => {
    for (const bad of ['red', 'rgb(1,2,3)', 'var(--x)', '#fff', '#12345g', '']) {
      expect(isRenderableColor(bad)).toBe(false)
      expect(cssVarsFor({ 'color.accent': bad })).toEqual({})
    }
  })

  it('applies a canonical six-digit hex', () => {
    expect(cssVarsFor({ 'color.accent': '#AABBCC' })).toEqual({ '--accent': '#AABBCC' })
  })
})

describe('the token map is closed', () => {
  it('ignores an id that is not in the exposed map', () => {
    // A compromised or stale server must not be able to name a property this
    // app never intended to expose.
    expect(cssVarsFor({ 'color.secret.thing': '#000000' })).toEqual({})
  })

  it('exposes only custom properties, never bare identifiers', () => {
    for (const prop of Object.values(CSS_VAR_BY_TOKEN)) {
      expect(prop.startsWith('--')).toBe(true)
    }
  })

  it('one bad token does not discard the good ones', () => {
    // A malformed value must not take theming down for everything else.
    const vars = cssVarsFor({
      'color.accent': '#123456',
      'color.canvas.bg': 'url(x)',
    })
    expect(vars).toEqual({ '--accent': '#123456' })
  })
})

describe('applying and undoing', () => {
  it('sets the properties on the root', () => {
    applyOverlay({ 'color.accent': '#101010' })
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('#101010')
  })

  it('removes EXACTLY what it set, so the committed default returns', () => {
    // The undo path is what pulls a denied or lapsed preview off the screen.
    const undo = applyOverlay({ 'color.accent': '#101010' })
    undo()
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('')
  })

  it('undo does not clobber a property it never set', () => {
    // A snapshot-restore undo would roll back a LATER overlay's value. This is
    // the same defect the server's revert path had.
    document.documentElement.style.setProperty('--canvas-bg', '#ffffff')
    const undo = applyOverlay({ 'color.accent': '#101010' })
    undo()
    expect(document.documentElement.style.getPropertyValue('--canvas-bg')).toBe('#ffffff')
  })

  it('undoing twice is harmless', () => {
    const undo = applyOverlay({ 'color.accent': '#101010' })
    undo()
    expect(() => undo()).not.toThrow()
  })
})

describe('copy', () => {
  it('is returned for rendering as text, never applied to the DOM', () => {
    const before = document.documentElement.getAttribute('style')
    applyOverlay({ 'copy.home.title': '<img src=x onerror=alert(1)>' })
    expect(document.documentElement.getAttribute('style')).toBe(before)
  })

  it('overrides defaults and leaves untouched keys alone', () => {
    const copy = copyFor(
      { 'copy.home.title': 'Mine', 'color.accent': '#000000' },
      { 'copy.home.title': 'Default', 'copy.home.sub': 'Keep' },
    )
    expect(copy['copy.home.title']).toBe('Mine')
    expect(copy['copy.home.sub']).toBe('Keep')
    expect(copy['color.accent']).toBeUndefined()  // colour is not copy
  })
})

describe('degenerate input', () => {
  it('survives null and undefined without throwing', () => {
    expect(cssVarsFor(null)).toEqual({})
    expect(cssVarsFor(undefined)).toEqual({})
    expect(copyFor(null)).toEqual({})
  })
})
