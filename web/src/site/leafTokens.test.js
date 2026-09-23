// Slice 13b (theming tokens): the --leaf-* namespace is aliases ONLY. Every
// --leaf-* declaration in the site stylesheets must be a var() reference to
// a token declared in the very same selector scope (this slice never spans
// scopes), never a re-typed literal, so introducing the namespace changes no
// pixel or shade on screen.
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
  for (const [a, b] of [['foreground', 'background'], ['foreground', 'card'], ['muted', 'card'], ['primary', 'card'], ['on-accent', 'primary']]) {
    expect(contrast(a, b), `${a}/${b}`).toBeGreaterThanOrEqual(4.5)
  }
  expect(contrast('muted', 'card')).toBeGreaterThanOrEqual(3)
  expect(contrast('border', 'card')).toBeLessThan(3)
})

describe('the --leaf-* token namespace is alias-only (slice 13b)', () => {
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
