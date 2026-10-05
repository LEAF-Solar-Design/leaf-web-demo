import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const files = [
  'src/cadedit/drawingObjectList.css',
  'src/demo/demo.css',
  'src/site/cockpit.css',
  'src/site/landing.css',
]
const sheets = Object.fromEntries(files.map((file) => [file, readFileSync(`${process.cwd()}/${file}`, 'utf8')]))
const stripComments = (css) => css.replace(/\/\*[\s\S]*?\*\//g, '')
const compact = (value) => value.replace(/\s+/g, '')

// Keep media ancestry and selector lists intact: commas inside :is() are
// part of a selector, and a coarse rule nested in a media block must count.
function selectors(header) {
  const result = []
  let depth = 0
  let start = 0
  for (let i = 0; i < header.length; i += 1) {
    if (header[i] === '(' || header[i] === '[') depth += 1
    if (header[i] === ')' || header[i] === ']') depth -= 1
    if (header[i] === ',' && depth === 0) {
      result.push(header.slice(start, i).trim())
      start = i + 1
    }
  }
  result.push(header.slice(start).trim())
  return result
}

function cssRules(css, media = []) {
  const text = stripComments(css)
  const result = []
  let start = 0
  while (start < text.length) {
    const open = text.indexOf('{', start)
    if (open === -1) break
    let depth = 1
    let end = open + 1
    while (depth && end < text.length) {
      if (text[end] === '{') depth += 1
      if (text[end] === '}') depth -= 1
      end += 1
    }
    const header = text.slice(start, open).trim()
    const body = text.slice(open + 1, end - 1)
    if (header.startsWith('@media')) {
      result.push(...cssRules(body, [...media, header.slice(6).trim()]))
    } else if (!header.startsWith('@')) {
      const declarations = Object.fromEntries([...body.matchAll(/([\w-]+)\s*:\s*([^;]+);/g)]
        .map(([, name, value]) => [name, value.trim()]))
      for (const selector of selectors(header)) result.push({ selector, declarations, media })
    }
    start = end
  }
  return result
}

const rules = Object.fromEntries(files.map((file) => [file, cssRules(sheets[file])]))
const landing = rules['src/site/landing.css']
const demo = rules['src/demo/demo.css']
const skin = '.studio-shell .app:is([data-surface="cad"], [data-surface="solar"])'
const coarseRule = (file, selector) => rules[file].find((rule) =>
  rule.selector === selector && rule.media.includes('(pointer: coarse)'))
const animationFor = (selector) => landing.find((rule) =>
  rule.selector.startsWith(selector) && /^studioGround(?:In|Out)\b/.test(rule.declarations.animation || ''))

describe('S4 dense targets and viewport bounds', () => {
  it.each([
    ['src/cadedit/drawingObjectList.css', `${skin} .drawing-objects-panel > summary`],
    ['src/demo/demo.css', '.tour-card.is-coach .tour-coach-toggle'],
    ['src/demo/demo.css', '.tour-card.is-coach .tour-card-actions button'],
    ['src/site/cockpit.css', `${skin} .cockpit-prompt`],
  ])('%s gives %s a 44px coarse target', (file, selector) => {
    expect(coarseRule(file, selector)?.declarations['min-height']).toBe('44px')
  })

  it('the compact summary and tour controls also have 44px minimum widths', () => {
    for (const [file, selector] of [
      ['src/cadedit/drawingObjectList.css', `${skin} .drawing-objects-panel > summary`],
      ['src/demo/demo.css', '.tour-card.is-coach .tour-coach-toggle'],
      ['src/demo/demo.css', '.tour-card.is-coach .tour-card-actions button'],
    ]) expect(coarseRule(file, selector)?.declarations['min-width']).toBe('44px')
    const controls = coarseRule('src/site/cockpit.css', `${skin} .cockpit-prompt :is(.cp-input, .cp-mode, .cp-run, .cp-cancel, .cp-field)`)
    expect(controls?.declarations['min-height']).toBe('44px')
    expect(controls?.declarations['min-width']).toBe('44px')
  })

  it.each(files)('%s carries no 100vh viewport bound', (file) => {
    expect(sheets[file]).not.toMatch(/\b100vh\b/)
  })
})

describe('S4 entrance and exit motion', () => {
  it.each([
    ['.studio-ground > [data-ground-phase="entering"]', 'studioGroundIn 180ms cubic-bezier(.22,1,.36,1) both'],
    ['.studio-shell .app[data-studio-transition="in"]', 'studioGroundIn 140ms cubic-bezier(.22,1,.36,1) both'],
    ['.studio-ground > [data-ground-phase="leaving"]', 'studioGroundOut 180ms ease both'],
    ['.studio-shell .app[data-studio-transition="out"]', 'studioGroundOut 80ms ease both'],
  ])('%s uses the ratified curve and retains its duration', (selector, animation) => {
    expect(compact(animationFor(selector)?.declarations.animation || '')).toBe(compact(animation))
  })

  it('landing contains neither the old curve nor blanket transitions', () => {
    const text = compact(stripComments(sheets['src/site/landing.css']))
    expect(text).not.toMatch(/cubic-bezier\(0?\.2,0,0,1\)/)
    expect(stripComments(sheets['src/site/landing.css'])).not.toMatch(/\btransition\s*:\s*all\b/)
    const marker = landing.find((rule) => rule.selector === '.ground-world-marker' && rule.declarations.transition)
    const properties = selectors(marker?.declarations.transition || '').map((leg) => leg.split(/\s+/)[0])
    expect(properties).toEqual(['left', 'top', 'width', 'height'])
  })

  it('all tour animations use the shared curve, including the waiting pulse', () => {
    for (const selector of ['.tour-dim', '.tour-card', '.tour-card-wait::before']) {
      const rule = demo.find((candidate) => candidate.selector === selector && candidate.declarations.animation)
      expect(rule?.declarations.animation).toContain('var(--m-curve)')
    }
    expect(demo.find((rule) => rule.selector === '.tour-card.is-centered')?.declarations['animation-name'])
      .toBe('tour-rise-centered')
  })

  it('the caption retains a nonzero opacity transition without reduced motion', () => {
    for (const selector of ['.tc-caption', '.stage-root[data-scene="tool"] .tc-caption']) {
      const rule = landing.find((candidate) => candidate.selector === selector && candidate.media.length === 0)
      expect(rule?.declarations.transition).toMatch(/^opacity\s+/)
      const duration = rule?.declarations.transition.match(/(\d*\.?\d+)(ms|s)\b/)
      expect(Number(duration?.[1])).toBeGreaterThan(0)
    }
  })
})

describe('S4 tokens and mouse affordances', () => {
  it('green literals occur only in a token definition outside the alias namespace', () => {
    const definitions = []
    for (const [file, fileRules] of Object.entries(rules)) {
      for (const rule of fileRules) {
        for (const [name, value] of Object.entries(rule.declarations)) {
          if (!/#7fd6a6\b/i.test(value)) continue
          expect(name, `${file}: ${rule.selector}`).toMatch(/^--(?!leaf-)/)
          definitions.push(name)
        }
      }
    }
    expect(definitions).toEqual(['--studio-accent'])
    expect(landing.find((rule) => rule.selector === ':root')?.declarations['--studio-chip-radius']).toBe('6px')
    expect(landing.find((rule) => rule.selector === '.tc-product-columns li')?.declarations['border-radius'])
      .toBe('var(--studio-chip-radius)')
  })

  it('landing hover paint is gated to a fine pointer with hover', () => {
    const hoverRules = landing.filter((rule) => rule.selector.includes(':hover'))
    expect(hoverRules.length).toBeGreaterThan(0)
    for (const rule of hoverRules) {
      expect(rule.media, rule.selector).toContain('(hover: hover) and (pointer: fine)')
    }
    const activeVersion = landing.find((rule) => rule.selector === '.tc-version-row > button:first-child.active')
    expect(activeVersion?.media).toEqual([])
    expect(activeVersion?.declarations.background).toBe('rgba(34, 197, 94, .07)')
  })

  it('the append-only cockpit restores resting paint outside the mouse gate', () => {
    const resets = rules['src/site/cockpit.css'].filter((rule) => rule.media.includes('(hover: none), (pointer: coarse), (pointer: none)'))
    for (const control of ['.cockpit-quick button', '.cockpit-ribbon-tabs', '.ribbon-cluster-label', '.ribbon-tool',
      '.doc-tab-start', '.doc-tab-close', '.doc-tab-add', '.tc-product-tabs', '.cockpit-view button', '.dock-close',
      '.bar-command-line .bar-controls', '.cp-run', '.cp-cancel', '.cp-mode', '.foot-doc-add',
      '.cockpit-status-toggles button', '.properties-dock .legend-row']) {
      expect(resets.some((rule) => rule.selector.includes(control)), control).toBe(true)
    }
  })
})
