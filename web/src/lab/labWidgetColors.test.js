// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const readCSS = path => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\/\*[\s\S]*?\*\//g, '')
const styles = {
  'widgets.css': readCSS('./widgets/widgets.css'),
  'labPreview.css': readCSS('./labPreview.css'),
}
const rules = css => [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(([, selector, body]) => ({ selector: selector.trim(), body }))
const declarations = body => [...body.matchAll(/(?:^|;)\s*([\w-]+)\s*:\s*([^;]+)/g)]
  .map(([, property, value]) => [property, value.trim()])
const ruleBody = (css, selector) => {
  const matches = rules(css).filter(rule => rule.selector === selector)
  expect(matches, selector).toHaveLength(1)
  return matches[0].body
}
const propertyValue = (body, property) => {
  const matches = declarations(body).filter(([name]) => name === property)
  expect(matches, property).toHaveLength(1)
  return matches[0][1]
}

const luminance = hex => {
  expect(hex).toMatch(/^#[\da-f]{6}$/i)
  const channels = hex.slice(1).match(/../g).map(channel => {
    const value = parseInt(channel, 16) / 255
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4
  })
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
}

describe('Lab widget v1 color roles', () => {
  it('reads the cockpit shell roles directly without legacy palette variables', () => {
    const shellRules = rules(readCSS('../site/studioShell.css')).filter(({ selector, body }) =>
      selector === '.studio-shell .app[data-studio-shell="cockpit"][data-surface]'
      && declarations(body).some(([property]) => property === '--card'))
    expect(shellRules).toHaveLength(1)
    const shell = shellRules[0].body
    const migrated = [
      ['widgets.css', '.widgets-view', 'color', '--foreground', ''],
      ['widgets.css', '.widgets-view', 'background', '--card', ''],
      ['widgets.css', '.widgets-view', 'border', '--border', '1px solid '],
      ['widgets.css', '.widgets-view button, .widgets-view input, .widgets-view textarea, .widgets-view select', 'background', '--card', ''],
      ['widgets.css', '.widgets-view button[aria-pressed="true"]', 'border-color', '--primary', ''],
      ['widgets.css', '.widgets-view button[aria-pressed="true"]', 'background', '--accent-tint', ''],
      ['widgets.css', '.widgets-view :is(button, input, textarea, select):focus-visible', 'outline', '--primary', '3px solid '],
      ['widgets.css', '.widget-layer', 'border-bottom', '--border', '1px solid '],
      ['labPreview.css', '.lab-preview', 'color', '--foreground', ''],
      ['labPreview.css', '.lab-preview', 'background', '--card', ''],
      ['labPreview.css', '.lab-preview', 'border', '--border', '1px solid '],
      ['labPreview.css', '.lab-preview-toggle, .lab-preview [role="tab"]', 'background', '--card', ''],
      ['labPreview.css', '.lab-preview [aria-selected="true"], .lab-preview-toggle[aria-expanded="true"]', 'border-color', '--primary', ''],
      ['labPreview.css', '.lab-preview [aria-selected="true"], .lab-preview-toggle[aria-expanded="true"]', 'background', '--accent-tint', ''],
      ['labPreview.css', '.lab-preview :is(button, input, textarea, select):focus-visible', 'outline', '--primary', '3px solid '],
    ]
    for (const name of ['--foreground', '--card', '--border', '--primary', '--accent-tint']) {
      let role = propertyValue(shell, name)
      const cockpitAlias = role.match(/^var\((--ck-[\w-]+)\)$/)
      if (cockpitAlias) role = propertyValue(shell, cockpitAlias[1])
      expect(role, name).toMatch(/^var\(--leaf-[\w-]+\)$/)
      for (const [file, css] of Object.entries(styles)) {
        expect(css, `${file} ${name}`).not.toMatch(new RegExp(`var\\(\\s*${name}(?=\\s*[,\\)])`))
        const uses = migrated.filter(([source, , , legacy]) => source === file && legacy === name)
        expect(uses.length, `${file} ${name}`).toBeGreaterThan(0)
        for (const [, selector, property, , prefix] of uses) {
          expect(propertyValue(ruleBody(css, selector), property), `${file} ${selector} ${property}`)
            .toBe(`${prefix}${role}`)
        }
      }
    }
  })

  it.each([
    ['widgets.css', '.widgets-view button, .widgets-view input, .widgets-view textarea, .widgets-view select'],
    ['labPreview.css', '.lab-preview-toggle, .lab-preview [role="tab"]'],
  ])('%s gives controls a contrasting border', (file, selector) => {
    expect(propertyValue(ruleBody(styles[file], selector), 'border'))
      .toBe('1px solid var(--leaf-ink-muted)')
  })

  it('uses the error role for invalid controls', () => {
    expect(propertyValue(ruleBody(styles['widgets.css'], '.widgets-view [aria-invalid="true"]'), 'border-color'))
      .toBe('var(--leaf-error)')
  })

  it('maps each vendored token fallback to its intended v1 role', () => {
    const expected = [
      ['.widget-designer-preview', 'background', 'var(--leaf-color-background, var(--leaf-surface))'],
      ['.widget-designer-preview p', 'color', 'var(--leaf-color-foreground, var(--leaf-ink))'],
      ['.widget-designer-surface:focus-visible', 'outline', '2px solid var(--leaf-color-success, var(--leaf-ok))'],
      ['.widget-selection', 'stroke', 'var(--leaf-color-success, var(--leaf-ok))'],
      ['.widget-handle', 'fill', 'var(--leaf-color-background, var(--leaf-surface))'],
      ['.widget-handle', 'stroke', 'var(--leaf-color-success, var(--leaf-ok))'],
      ['.widget-guide', 'stroke', 'var(--leaf-color-data-series-3, var(--leaf-warn))'],
      ['.widget-cutout', 'fill', 'var(--leaf-color-background, var(--leaf-ground))'],
    ]
    for (const [selector, property, value] of expected) {
      expect(propertyValue(ruleBody(styles['widgets.css'], selector), property), `${selector} ${property}`)
        .toBe(value)
    }
    expect([...styles['widgets.css'].matchAll(/var\(\s*--leaf-color-[\w-]+\s*,/g)])
      .toHaveLength(expected.length)
  })

  it('keeps only the device canvas color literal and preserves its corner', () => {
    const literals = []
    for (const [file, css] of Object.entries(styles)) {
      for (const { selector, body } of rules(css)) {
        for (const [property, value] of declarations(body)) {
          for (const [literal] of value.matchAll(/#[\da-f]+\b|\b(?:rgba?|hsla?)\([^)]*\)/gi)) {
            literals.push({ file, selector, property, literal })
          }
        }
      }
    }
    expect(literals).toEqual([
      { file: 'widgets.css', selector: '.widget-preview-canvas', property: 'background', literal: '#09090b' },
    ])
    const canvas = ruleBody(styles['widgets.css'], '.widget-preview-canvas')
    expect(propertyValue(canvas, 'background')).toBe('#09090b')
    expect(propertyValue(canvas, 'border-radius')).toBe('22px')
  })

  it.each([
    ['--leaf-ink', '--leaf-ground', 4.5],
    ['--leaf-ink-muted', '--leaf-ground', 3],
    ['--leaf-accent', '--leaf-ground', 3],
    ['--leaf-error', '--leaf-ground', 3],
    ['--leaf-ok', '--leaf-surface', 3],
  ])('%s on %s meets %s:1 contrast', (foreground, background, minimum) => {
    const root = rules(readCSS('../standard/leaf-standard.css')).find(rule => rule.selector === ':root')
    expect(root).toBeDefined()
    const values = [foreground, background].map(role => luminance(propertyValue(root.body, role)))
    const contrast = (Math.max(...values) + 0.05) / (Math.min(...values) + 0.05)
    expect(contrast).toBeGreaterThanOrEqual(minimum)
  })
})
