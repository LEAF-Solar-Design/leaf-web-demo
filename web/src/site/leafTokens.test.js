// Slice 13b (theming tokens): the --leaf-* namespace is aliases ONLY. Every
// --leaf-* declaration in the site stylesheets must be a var() reference to
// a token declared in the very same selector scope (this slice never spans
// scopes), never a re-typed literal. The cockpit inherits --leaf-ink and
// --leaf-warn from the vendored v1 package instead of aliasing them back.
import { readFileSync } from 'node:fs'
import { describe, it, expect } from 'vitest'

const read = (p) => readFileSync(`${process.cwd()}/${p}`, 'utf8')
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')

// Every occurrence of `selector { ... }`, brace-depth balanced so a selector
// that recurs (e.g. inside an @media block elsewhere in the file) is
// captured whole rather than cut off at its first nested close-brace.
function declsFor(css, selector) {
  const text = stripComments(css)
  const decls = {}
  let idx = 0
  for (;;) {
    const i = text.indexOf(selector, idx)
    if (i === -1) break
    const j = text.indexOf('{', i)
    if (j === -1) break
    if (!/^\s*$/.test(text.slice(i + selector.length, j))) { idx = i + 1; continue }
    let depth = 1
    let k = j + 1
    while (depth && k < text.length) {
      if (text[k] === '{') depth += 1
      else if (text[k] === '}') depth -= 1
      k += 1
    }
    const body = text.slice(j + 1, k - 1)
    const declRe = /(--[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g
    let m
    while ((m = declRe.exec(body))) decls[m[1]] = m[2].trim()
    idx = k
  }
  return decls
}

function allLeafDecls(css) {
  const text = stripComments(css)
  const out = []
  const declRe = /(--leaf-[a-zA-Z0-9-]+)\s*:\s*([^;]+);/g
  let m
  while ((m = declRe.exec(text))) out.push([m[1], m[2].trim()])
  return out
}

const landing = read('src/site/landing.css')
const cockpit = read('src/site/cockpit.css')

const SCOPES = [
  { file: 'landing.css', css: landing, selector: '.leaf-light' },
  { file: 'landing.css', css: landing, selector: '.stage-root' },
  { file: 'cockpit.css', css: cockpit, selector: '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"])' },
]

it('SSD1-24E light tokens match the ratified paper identity', () => {
  const light = declsFor(landing, '.leaf-light')
  const paper = declsFor(read('src/styles.css'), ':root')
  for (const name of ['background', 'foreground', 'card', 'border', 'primary', 'primary-hover', 'on-accent', 'muted', 'muted-2']) {
    expect(light[`--${name}`]).toMatch(/^#[0-9a-f]{6}$/i)
    expect(light[`--${name}`].toLowerCase()).toBe(paper[`--${name}`].toLowerCase())
  }
})

it('SSD1-24E light text and tile boundaries meet WCAG contrast', () => {
  const light = declsFor(landing, '.leaf-light')
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => {
      const srgb = parseInt(value, 16) / 255
      return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
    })
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
  }
  const contrast = (a, b) => {
    const values = [a, b].map((name) => luminance(light[`--${name}`])).sort((x, y) => y - x)
    return (values[0] + 0.05) / (values[1] + 0.05)
  }
  for (const [a, b] of [['foreground', 'background'], ['foreground', 'card'], ['muted', 'card'], ['primary', 'card'], ['on-accent', 'primary'], ['on-accent', 'primary-hover']]) {
    expect(contrast(a, b), `${a}/${b}`).toBeGreaterThanOrEqual(4.5)
  }
  expect(contrast('muted', 'card')).toBeGreaterThanOrEqual(3)
  expect(contrast('border', 'card')).toBeLessThan(3)
})

it('cockpit accent backgrounds use dark text from the vendored v1 root', () => {
  const root = declsFor(read('src/standard/leaf-standard.css'), ':root')
  expect(root['--leaf-on-accent']).toBe('#050706')
  expect(root['--leaf-accent']).toBe('#28d66f')

  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => {
      const srgb = parseInt(value, 16) / 255
      return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
    })
    return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
  }
  const contrast = (a, b) => {
    const values = [a, b].map(luminance).sort((x, y) => y - x)
    return (values[0] + 0.05) / (values[1] + 0.05)
  }
  expect(contrast(root['--leaf-on-accent'], root['--leaf-accent'])).toBeGreaterThanOrEqual(4.5)

  for (const [file, css] of [['cockpit.css', cockpit], ['studioShell.css', read('src/site/studioShell.css')]]) {
    const text = stripComments(css)
    expect(text, `${file} must inherit --leaf-on-accent`).not.toMatch(/--leaf-on-accent\s*:/)
    for (const [, selector, body] of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      if (!/(?:^|;)\s*background\s*:\s*var\(--ck-(?:active|cmd)\)\s*(?:;|$)/.test(body)) continue
      const context = `${file} ${selector.trim()}`
      const colors = [...body.matchAll(/(?:^|;)\s*color\s*:\s*([^;]+)/g)].map((match) => match[1].trim())
      // Accent dots without a color declaration carry no text and are out of scope.
      if (colors.length === 0) continue
      for (const color of colors) {
        expect(color, context).not.toMatch(/#fff\b|rgba\(\s*255\s*,\s*255\s*,\s*255\b/i)
        if (color === 'var(--leaf-on-accent)') continue
        expect(color, `${context} must use --leaf-on-accent or a dark literal`).toMatch(/^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i)
        const hex = color.length === 4 ? `#${[...color.slice(1)].map((channel) => channel.repeat(2)).join('')}` : color
        expect(luminance(hex), context).toBeLessThan(luminance(root['--leaf-accent']))
        expect(contrast(hex, root['--leaf-accent']), context).toBeGreaterThanOrEqual(4.5)
      }
    }
  }
})

it('SSD1-24E light hover text', () => {
  const css = stripComments(read('src/site/landing.css'))
  const rules = [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  const scope = '.studio-ground-board[data-ground="browser"][data-board-theme="light"]'
  for (const action of ['.ground-row-action:hover *', '.ground-theme-toggle:hover *']) {
    expect(rules.some(([, selectors, declarations]) =>
      selectors.split(',').some((selector) => selector.trim() === `${scope} ${action}`)
      && /(?:^|;)\s*color\s*:\s*var\(--leaf-on-accent\)\s*;/.test(declarations),
    ), action).toBe(true)
  }
})

describe('the --leaf-* token namespace is alias-only (slice 13b)', () => {
  it('inherits cockpit ink and warning roles from the vendored v1 root', () => {
    const root = declsFor(read('src/standard/leaf-standard.css'), ':root')
    for (const [css, selector] of [
      [cockpit, '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"])'],
      [read('src/site/studioShell.css'), '.studio-shell .app[data-studio-shell="cockpit"][data-surface]'],
    ]) {
      const decls = declsFor(css, selector)
      for (const role of ['ink', 'warn']) {
        expect(root).toHaveProperty(`--leaf-${role}`)
        expect(decls).not.toHaveProperty(`--leaf-${role}`)
        expect(decls[`--ck-${role}`]).toBe(`var(--leaf-${role})`)
      }
    }
  })

  it('declares no --leaf-* literal colour or length, only var() references', () => {
    const literalRe = /#[0-9a-fA-F]{3,8}\b|\b\d+(\.\d+)?(px|em|rem|vh|vw|%)\b|rgba?\(|hsla?\(/
    for (const [file, css] of [['landing.css', landing], ['cockpit.css', cockpit]]) {
      for (const [name, value] of allLeafDecls(css)) {
        expect(value.startsWith('var('), `${file} ${name}: "${value}" is not a var() reference`).toBe(true)
        expect(literalRe.test(value), `${file} ${name}: "${value}" carries a literal value`).toBe(false)
      }
    }
  })

  it('resolves every --leaf-* alias to a token declared in its own scope', () => {
    for (const { file, css, selector } of SCOPES) {
      const decls = declsFor(css, selector)
      const leafNames = Object.keys(decls).filter((k) => k.startsWith('--leaf-'))
      expect(leafNames.length, `${file} ${selector}: no --leaf-* declarations found`).toBeGreaterThan(0)
      for (const name of leafNames) {
        const value = decls[name]
        const ref = value.match(/^var\((--[a-zA-Z0-9-]+)\)$/)
        expect(ref, `${file} ${selector} ${name}: "${value}" is not a plain var() reference`).not.toBeNull()
        const target = ref[1]
        expect(target in decls, `${file} ${selector} ${name} -> ${target}: target not declared in this scope`).toBe(true)
      }
    }
  })
})
