/**
 * T1 preview render. Each test names the failure it prevents; the sink test is
 * the one an adversarial review found in the card's equivalent code.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

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

  it('mirrors the server registry colour vocabulary exactly (read from source)', () => {
    // CROSS-BOUNDARY on purpose. An earlier version of this test compared the
    // client map against a second hard-coded client list, so it passed happily
    // while the registry drifted underneath both — exactly the failure it was
    // written to catch (the shipped map had client-only surface.* tokens and
    // left panel.*/accent.fg/border unmapped, so five of seven approved tokens
    // set nothing). This parses the REGISTRY block out of
    // server/overlay_registry.py, so adding a token there and not here fails.
    // Resolved from the vitest root (web/), not import.meta.url — under the
    // jsdom environment import.meta.url is an http: URL and readFileSync
    // rejects it.
    const registryPath = resolve(process.cwd(), '../server/overlay_registry.py')
    const src = readFileSync(registryPath, 'utf8')
    const block = src.slice(src.indexOf('REGISTRY: Dict[str, TokenSpec]'),
                            src.indexOf('CONTRAST_PAIRS'))
    const serverColorTokens = [...block.matchAll(/_color\("([^"]+)"/g)].map((m) => m[1])
    expect(serverColorTokens.length).toBeGreaterThan(0)  // the parse itself must work
    expect(Object.keys(CSS_VAR_BY_TOKEN).sort()).toEqual([...serverColorTokens].sort())
  })

  it('pins EVERY token-to-property mapping, not just a sample', () => {
    // The map is the whole repaint contract; a silent re-point (say accent ->
    // a var no rule reads) would be invisible without this.
    expect(CSS_VAR_BY_TOKEN).toEqual({
      'color.canvas.bg': ['--background'],
      'color.canvas.fg': ['--foreground'],
      'color.panel.bg': ['--card', '--card-grad'],
      'color.panel.fg': ['--panel-fg'],
      'color.accent': ['--primary'],
      'color.accent.fg': ['--on-accent'],
      'color.border': ['--border'],
    })
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

describe('the panel foreground is independently seeded', () => {
  // The server validates color.panel.fg on its own, against PLATFORM_DEFAULTS.
  // If the stylesheet left panel text to INHERIT --foreground, a proposal that
  // recoloured only the canvas would clear the server's panel contrast pair
  // (measured against its white default) while panels rendered the overlaid
  // canvas text on the panel background — 1.19:1 against a 4.5:1 floor
  // (sol-critic PR #439 round 8).
  const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

  it('seeds --panel-fg to the SAME value the server defaults it to', () => {
    const registry = readFileSync(
      resolve(process.cwd(), '../server/overlay_registry.py'), 'utf8')
    const block = registry.slice(registry.indexOf('PLATFORM_DEFAULTS'),
                                 registry.indexOf('def defaults('))
    const serverPanelFg = /"color\.panel\.fg":\s*"(#[0-9a-fA-F]{6})"/.exec(block)
    expect(serverPanelFg).not.toBeNull()
    const seeded = /--panel-fg:\s*(#[0-9a-fA-F]{6})/.exec(css)
    expect(seeded).not.toBeNull()
    expect(seeded[1].toLowerCase()).toBe(serverPanelFg[1].toLowerCase())
  })

  it('never leaves panel text to inherit', () => {
    // `var(--panel-fg, inherit)` was the original shape and is exactly the bug.
    expect(css.includes('var(--panel-fg, inherit)')).toBe(false)
  })

  it('paints panel text wherever it paints the panel background', () => {
    // A background rule without the matching colour rule is a domain gap: the
    // fg token would govern less than the bg token, which is what let a valid
    // pair render white-on-white.
    const rules = css.split('}')
    const gaps = rules.filter((rule) =>
      /background:\s*var\(--card-grad\)|background:\s*var\(--card\)/.test(rule)
      && !/color:\s*var\(--panel-fg\)/.test(rule))
    expect(gaps).toEqual([])
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
