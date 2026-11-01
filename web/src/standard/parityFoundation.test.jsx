import { readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'

const read = (path) => readFileSync(`${process.cwd()}/src/${path}`, 'utf8')
const manifest = JSON.parse(read('standard/SOURCES.json'))
const foundation = read('standard/parity-foundation.css')

describe('Studio parity foundation', () => {
  it('consumes the pinned Leaf standard v1.3.0 release', () => {
    const bytes = readFileSync(`${process.cwd()}/src/standard/leaf-standard.css`)
    const css = bytes.toString('utf8')
    expect(createHash('sha256').update(bytes).digest('hex')).toBe(manifest.sha256)
    expect(bytes.length).toBe(manifest.bytes)
    expect(css).toContain('--leaf-line-control: #737e76')
    expect(css.split('\n')[1]).toContain('1.3.0')
  })

  it('imports the foundation immediately after the Studio stylesheet', () => {
    const main = read('main.jsx')
    const studioIndex = main.indexOf("'./styles.css'")
    const foundationIndex = main.indexOf("'./standard/parity-foundation.css'")
    expect(studioIndex).toBeGreaterThanOrEqual(0)
    expect(foundationIndex).toBeGreaterThan(studioIndex)
    expect(main).toMatch(/import '\.\/styles\.css'\r?\nimport '\.\/standard\/parity-foundation\.css'/)
  })

  it('contains the ghost recipe without shell selectors', () => {
    expect(foundation.split('\n')[0]).toBe('/* leaf-dp-v1 */')
    expect(foundation).toContain('.btn.ghost{background:transparent')
    for (const selector of ['header', '.top', '.ribbon', '.foot-bar', '.tab', '.status']) {
      expect(foundation).not.toContain(selector)
    }
  })

  it('gives a ghost button a transparent computed background', () => {
    const style = document.createElement('style')
    const button = document.createElement('button')
    style.textContent = foundation
    button.className = 'btn ghost'
    button.textContent = 'Secondary action'
    document.head.appendChild(style)
    document.body.appendChild(button)
    try {
      expect(['transparent', 'rgba(0, 0, 0, 0)']).toContain(getComputedStyle(button).backgroundColor)
    } finally {
      button.remove()
      style.remove()
    }
  })
})
