// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const read = path => readFileSync(new URL(path, import.meta.url), 'utf8')
const stripComments = source => source.replace(/\/\*[\s\S]*?\*\//g, '')
const labCSS = read('./lab.css')
const adapterCSS = read('./studioParityAdapter.css')
const standardCSS = read('../standard/leaf-standard.css')
const tokensCSS = read('./contract/sources/leaf-tokens.css')

function rules(source) {
  return [...stripComments(source).matchAll(/([^{}]+)\{([^{}]*)\}/g)].map(([, selector, body]) => ({
    selector: selector.trim(),
    declarations: body.split(';').map(value => value.trim()).filter(Boolean),
  }))
}

const adapterRules = rules(adapterCSS)

describe('Lab Studio parity adapter', () => {
  it('imports the adapter immediately after the layered tokens', () => {
    const statements = stripComments(labCSS).trim().split(';').slice(0, 2).map(value => `${value.trim()};`)
    expect(statements).toEqual([
      "@import './contract/sources/leaf-tokens.css' layer(leaf-lab-tokens);",
      "@import './studioParityAdapter.css';",
    ])
  })

  it('contains only the Studio workbench and focus rules', () => {
    expect(adapterRules.map(rule => rule.selector)).toEqual([
      '[data-leaf-profile="studio"] .lab-workbench',
      '[data-leaf-profile="studio"] .lab-workbench :is(button,[role="button"],a):focus-visible',
    ])
    expect(stripComments(adapterCSS).replace(/[^{}]+\{[^{}]*\}/g, '').trim()).toBe('')
  })

  it('maps exactly the nine legacy roles to canonical roles in order', () => {
    expect(adapterRules[0].declarations).toEqual([
      '--leaf-color-background: var(--leaf-ground)',
      '--leaf-color-surface: var(--leaf-surface)',
      '--leaf-color-border: var(--leaf-line)',
      '--leaf-color-foreground: var(--leaf-ink)',
      '--leaf-color-primary: var(--leaf-accent)',
      '--leaf-font-sans: var(--leaf-font-text)',
      '--leaf-font-mono: var(--leaf-font-data)',
      '--leaf-radius-panel: var(--leaf-radius-card)',
      '--leaf-spacing-panel-padding: var(--leaf-space-3)',
    ])
  })

  it('uses canonical focus metrics', () => {
    expect(adapterRules[1].declarations).toEqual([
      'outline: var(--leaf-metric-focus-width) solid var(--leaf-focus)',
      'outline-offset: var(--leaf-metric-focus-offset)',
    ])
  })

  it('leaves imports, shared control radius, and shell selectors alone', () => {
    for (const forbidden of ['@import', '!important', 'leaf-tokens.css', '--leaf-radius-control']) {
      expect(adapterCSS).not.toContain(forbidden)
    }
    for (const { selector } of adapterRules) {
      expect(selector).not.toMatch(/header|\.top|\.ribbon|\.foot-bar/i)
      expect(selector).not.toMatch(/mushy|ops/i)
    }
  })

  it.each([1, 2, 3, 4, 5])('preserves data series %s', series => {
    expect(adapterCSS).not.toContain(`--leaf-color-data-series-${series}`)
  })

  it.each(['mushy', 'ops'])('preserves the vendored %s profile', profile => {
    expect(rules(tokensCSS).some(({ selector }) => selector.split(',')
      .some(part => part.trim() === `[data-leaf-profile="${profile}"]`))).toBe(true)
    expect(adapterCSS).not.toContain(`[data-leaf-profile="${profile}"]`)
  })

  it('connects Studio background and surface to canonical Stage and Paper values', () => {
    const standardRules = rules(standardCSS)
    const stage = standardRules.find(rule => rule.selector === ':root')
    const paper = standardRules.find(rule => rule.selector === '[data-leaf-theme="paper"]')
    expect(stage).toBeDefined()
    expect(paper).toBeDefined()
    expect(stage.declarations).toContain('--leaf-ground: #050706')
    expect(stage.declarations).toContain('--leaf-surface: #0e110f')
    expect(paper.declarations).toContain('--leaf-ground: #e9e7e0')
    expect(adapterRules[0].declarations).toContain('--leaf-color-background: var(--leaf-ground)')
    expect(adapterRules[0].declarations).toContain('--leaf-color-surface: var(--leaf-surface)')
  })
})
