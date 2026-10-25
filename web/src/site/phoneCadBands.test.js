// @vitest-environment node
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const readCss = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
  .replace(/\r\n/g, '\n').replace(/\/\*[\s\S]*?\*\//g, '')
const cockpit = readCss('./cockpit.css')
const navigation = readCss('./drawingNavigation.css')
const scope = '.studio-shell .app[data-studio-shell="cockpit"][data-drawer][data-surface]'
const phoneBlocks = (css) => [...css.matchAll(/@media\s*\(\s*max-width\s*:\s*599px\s*\)\s*\{((?:[^{}]|\{[^{}]*\})*)\}/g)]
  .map(([, body]) => body).join('\n')
const ruleBody = (css, selector) => [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .filter(([, head]) => head.trim() === selector)
  .map(([, , body]) => body).join('\n')
const phone = phoneBlocks(cockpit)
const find = phoneBlocks(navigation)

describe('phone CAD bands (L1, L4)', () => {
  it('gives the document strip room for visible 44 px controls', () => {
    const strip = ruleBody(phone, `${scope} .viewer-toolbar`)
    expect(strip).toMatch(/(?:^|;)\s*height:\s*46px;/)
    expect(strip).toMatch(/overflow-x:\s*auto;/)
    expect(ruleBody(phone, `${scope} .viewer-toolbar .local-cad-connect`))
      .toMatch(/flex-shrink:\s*0\s*!important;/)
    expect(ruleBody(phone, `${scope} .viewer-toolbar :is(.doc-tab-add, .doc-tab-close)`))
      .toMatch(/min-height:\s*44px;/)
  })

  it('places the view strip and legend in separate rows over the drawing', () => {
    const view = ruleBody(phone, `${scope} .cockpit-view`)
    expect(view).toMatch(/(?:^|;)\s*top:\s*6px;/)
    expect(view).toMatch(/(?:^|;)\s*left:\s*8px;/)
    expect(ruleBody(phone, `${scope} .cockpit-view button`)).toMatch(/min-height:\s*44px;/)
    expect(ruleBody(phone, `${scope} .viewer-wrap .legend`)).toMatch(/(?:^|;)\s*top:\s*148px;/)
  })

  it('places Find below the view strip with 44 px input and submit targets', () => {
    const band = ruleBody(find, '.drawing-navigation')
    expect(band).toMatch(/top:\s*calc\(var\(--ck-canvas-top,\s*140px\)\s*\+\s*64px\);/)
    expect(band).toMatch(/(?:^|;)\s*height:\s*76px;/)
    expect(ruleBody(find, '.drawing-navigation .drawing-find-field')).toMatch(/min-height:\s*46px;/)
    for (const selector of ['input', '.drawing-find-submit']) {
      const control = ruleBody(find, `.drawing-navigation .drawing-find-field ${selector}`)
      expect(control).toMatch(/(?:^|;)\s*height:\s*44px;/)
      expect(control).toMatch(/min-height:\s*44px;/)
    }
  })

  it('preserves the base Find band height for desktop', () => {
    const base = navigation.slice(0, navigation.indexOf('@media'))
    expect(ruleBody(base, '.drawing-navigation')).toMatch(/(?:^|;)\s*height:\s*52px;/)
  })
})
