import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(`${process.cwd()}/${path}`, 'utf8')
const manifest = JSON.parse(read('src/standard/SOURCES.json'))
const fontFiles = [400, 500, 600, 700].map((weight) => `inter-tight-${weight}-latin.woff2`)
const expectedFonts = [...fontFiles, 'OFL-inter-tight.txt']
const expectedBrand = ['leaf-automation-horizontal-on-dark.svg', 'leaf-automation-horizontal-on-light.svg']

describe('vendored Leaf standard v1', () => {
  it('lists exactly the eight pinned assets and no extra Inter Tight or brand files', () => {
    expect(manifest.schema).toBe('leaf.vendored-sources.v1')
    expect(manifest.files.map(({ path }) => path).sort()).toEqual([
      'src/standard/leaf-standard.css',
      ...expectedFonts.map((name) => `public/fonts/${name}`),
      ...expectedBrand.map((name) => `public/brand/${name}`),
    ].sort())
    const actualFonts = readdirSync(`${process.cwd()}/public/fonts`)
      .filter((name) => name.startsWith('inter-tight') || name.startsWith('OFL-inter-tight'))
    expect(actualFonts.sort()).toEqual([...expectedFonts].sort())
    expect(readdirSync(`${process.cwd()}/public/brand`).sort()).toEqual([...expectedBrand].sort())
  })

  for (const entry of manifest.files) {
    it(`preserves the bytes and sha256 of ${entry.path}`, () => {
      const path = `${process.cwd()}/${entry.path}`
      expect(existsSync(path)).toBe(true)
      const bytes = readFileSync(path)
      expect(bytes.length).toBe(entry.bytes)
      expect(createHash('sha256').update(bytes).digest('hex')).toBe(entry.sha256)
    })
  }

  it('imports the standard exactly once before the Studio stylesheet', () => {
    const lines = read('src/main.jsx').split(/\r?\n/)
    const standardImports = lines.filter((line) => /^\s*import\s+['"]\.\/standard\/leaf-standard\.css['"];?\s*$/.test(line))
    expect(standardImports).toHaveLength(1)
    const standardIndex = lines.indexOf(standardImports[0])
    const studioIndex = lines.findIndex((line) => /^\s*import\s+['"]\.\/styles\.css['"];?\s*$/.test(line))
    expect(studioIndex).toBeGreaterThan(standardIndex)
  })

  it('names the header mark independently of its two decorative lockups', () => {
    const mark = read('src/App.jsx').match(/<div\b([^>]*\bclassName="mark"[^>]*)>([\s\S]*?)<\/div>/)
    expect(mark).not.toBeNull()
    expect(mark[1]).toMatch(/\brole="img"/)
    expect(mark[1]).toMatch(/\baria-label="Leaf Automation"/)
    const images = mark[2].match(/<img\b[^>]*>/g) || []
    expect(images).toHaveLength(2)
    expect(images.map((image) => image.match(/\bclassName="([^"]*)"/)?.[1].split(/\s+/)
      .filter((name) => name === 'mark-lockup-light' || name === 'mark-lockup-dark')))
      .toEqual([['mark-lockup-light'], ['mark-lockup-dark']])
    for (const image of images) {
      expect(image).toMatch(/\balt=""/)
      expect(image).toMatch(/\baria-hidden="true"/)
    }
  })

  it('declares exactly four normal, swap Inter Tight faces with local font files', () => {
    const css = read('src/styles.css').replace(/\/\*[\s\S]*?\*\//g, '')
    const faces = [...css.matchAll(/@font-face\s*\{([^{}]*)\}/g)]
      .map(([, body]) => body)
      .filter((body) => /font-family\s*:\s*['"]Inter Tight['"]\s*;/.test(body))
    expect(faces).toHaveLength(4)
    const weights = faces.map((body) => {
      expect(body).toMatch(/font-style\s*:\s*normal\s*;/)
      expect(body).toMatch(/font-display\s*:\s*swap\s*;/)
      const weight = Number(body.match(/font-weight\s*:\s*(\d+)\s*;/)?.[1])
      const src = body.match(/src\s*:\s*url\(['"](\/fonts\/[^'"]+)['"]\)\s*format\(['"]woff2['"]\)\s*;/)
      expect(src).not.toBeNull()
      expect(src[1]).toBe(`/fonts/inter-tight-${weight}-latin.woff2`)
      expect(existsSync(`${process.cwd()}/public${src[1]}`)).toBe(true)
      return weight
    })
    expect(weights.sort((a, b) => a - b)).toEqual([400, 500, 600, 700])
  })

  it('keeps the dark default and explicit Paper sanity anchors', () => {
    const css = read('src/standard/leaf-standard.css').replace(/\/\*[\s\S]*?\*\//g, '')
    const root = css.match(/:root\s*\{([^{}]*)\}/)?.[1]
    const paper = css.match(/\[data-leaf-theme="paper"\]\s*\{([^{}]*)\}/)?.[1]
    expect(root).toMatch(/(?:^|;)\s*color-scheme\s*:\s*dark\s*;/)
    expect(root).toMatch(/(?:^|;)\s*--leaf-ground\s*:\s*#050706\s*;/)
    expect(paper).toMatch(/(?:^|;)\s*color-scheme\s*:\s*light\s*;/)
    expect(paper).toMatch(/(?:^|;)\s*--leaf-accent\s*:\s*#188143\s*;/)
  })
})
