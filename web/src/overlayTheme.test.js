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
    expect(cssVarsFor({ 'color.accent': '#AABBCC' })).toEqual({ '--primary': '#AABBCC' })
  })
})

describe('the token map is closed', () => {
  it('ignores an id that is not in the exposed map', () => {
    // A compromised or stale server must not be able to name a property this
    // app never intended to expose.
    expect(cssVarsFor({ 'color.secret.thing': '#000000' })).toEqual({})
  })

  it('exposes only custom properties, never bare identifiers', () => {
    for (const props of Object.values(CSS_VAR_BY_TOKEN)) {
      for (const prop of props) expect(prop.startsWith('--')).toBe(true)
    }
  })

  it('mirrors the server registry colour vocabulary exactly', () => {
    // server/overlay_registry.py REGISTRY is the source of truth; this list is
    // its mirror. The first shipped map drifted (surface.* client-side,
    // panel.*/accent.fg/border unmapped) and five of seven approved tokens
    // silently set nothing. Either side changing without the other fails here.
    const REGISTRY_COLOR_TOKENS = [
      'color.canvas.bg', 'color.canvas.fg',
      'color.panel.bg', 'color.panel.fg',
      'color.accent', 'color.accent.fg',
      'color.border',
    ]
    expect(Object.keys(CSS_VAR_BY_TOKEN).sort()).toEqual(REGISTRY_COLOR_TOKENS.sort())
  })

  it('maps tokens to the REAL stylesheet tokens, and panel.bg to both card vars', () => {
    // Inline :root properties win over the stylesheet declarations, so setting
    // the real tokens is the entire repaint mechanism; --canvas-bg-style names
    // that no rule reads were the original dead-end.
    expect(cssVarsFor({ 'color.canvas.bg': '#111111' })).toEqual({ '--background': '#111111' })
    expect(cssVarsFor({ 'color.panel.bg': '#222222' }))
      .toEqual({ '--card': '#222222', '--card-grad': '#222222' })
    expect(cssVarsFor({ 'color.border': '#333333' })).toEqual({ '--border': '#333333' })
  })

  it('one bad token does not discard the good ones', () => {
    // A malformed value must not take theming down for everything else.
    const vars = cssVarsFor({
      'color.accent': '#123456',
      'color.canvas.bg': 'url(x)',
    })
    expect(vars).toEqual({ '--primary': '#123456' })
  })
})

describe('applying and undoing', () => {
  it('sets the properties on the root', () => {
    applyOverlay({ 'color.accent': '#101010' })
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('#101010')
  })

  it('removes EXACTLY what it set, so the committed default returns', () => {
    // The undo path is what pulls a denied or lapsed preview off the screen.
    const undo = applyOverlay({ 'color.accent': '#101010' })
    undo()
    expect(document.documentElement.style.getPropertyValue('--primary')).toBe('')
  })

  it('undo does not clobber a property it never set', () => {
    // A snapshot-restore undo would roll back a LATER overlay's value. This is
    // the same defect the server's revert path had.
    document.documentElement.style.setProperty('--background', '#ffffff')
    const undo = applyOverlay({ 'color.accent': '#101010' })
    undo()
    expect(document.documentElement.style.getPropertyValue('--background')).toBe('#ffffff')
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
