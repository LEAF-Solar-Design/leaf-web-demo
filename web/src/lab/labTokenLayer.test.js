// @vitest-environment node
import { readFileSync, readdirSync } from 'node:fs'
import { extname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { preprocessCSS, resolveConfig } from 'vite'

const webDir = fileURLToPath(new URL('../../', import.meta.url))
const srcDir = fileURLToPath(new URL('../', import.meta.url))
const labPath = fileURLToPath(new URL('./lab.css', import.meta.url))
const sourcesDir = fileURLToPath(new URL('./contract/sources', import.meta.url))
const testPath = fileURLToPath(import.meta.url)
const labCSS = readFileSync(labPath, 'utf8')
const stripComments = source => source.replace(/\/\*[\s\S]*?\*\//g, '')

function sourceFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name)
    if (entry.name === 'node_modules' || path === sourcesDir) return []
    if (entry.isDirectory()) return sourceFiles(path)
    return entry.isFile() && ['.css', '.js', '.jsx', '.mjs'].includes(extname(path))
      ? [path]
      : []
  })
}

describe('Lab token cascade layer', () => {
  it('imports vendored tokens into the layer as its first statement', () => {
    const firstStatement = stripComments(labCSS).trim().match(/^[^;]*;/)?.[0]
    expect(firstStatement).toBe("@import './contract/sources/leaf-tokens.css' layer(leaf-lab-tokens);")
  })

  it('keeps lab.css as the only source referencing the vendored token stylesheet', () => {
    const references = sourceFiles(srcDir)
      .filter(path => path !== labPath && path !== testPath)
      .filter(path => readFileSync(path, 'utf8').includes('leaf-tokens.css'))
      .map(path => relative(srcDir, path))
    expect(references).toEqual([])
  })

  it('keeps vendored tokens limited to custom property declarations', () => {
    const tokens = stripComments(readFileSync(join(sourcesDir, 'leaf-tokens.css'), 'utf8'))
    expect(tokens).not.toContain('@')
    expect(tokens).not.toMatch(/!\s*important\b/i)
    const rules = [...tokens.matchAll(/\{([^{}]*)\}/g)]
    expect(rules.length).toBeGreaterThan(0)
    for (const [, body] of rules) {
      const declarations = body.split(';').map(value => value.trim()).filter(Boolean)
      expect(declarations.length).toBeGreaterThan(0)
      for (const declaration of declarations) {
        expect(declaration).toMatch(/^--[\w-]+\s*:/)
      }
    }
  })

  it('preserves the layer when Vite inlines the vendored tokens', async () => {
    const config = await resolveConfig({ root: webDir, configFile: false, logLevel: 'silent' }, 'build')
    const out = await preprocessCSS(labCSS, labPath, config)
    const layer = /@layer\s+leaf-lab-tokens\s*\{/.exec(out.code)
    expect(layer).not.toBeNull()
    const layerOpeningEnd = layer.index + layer[0].length
    const radiusReferences = [...out.code.matchAll(/--leaf-radius-control\b/g)]
    expect(radiusReferences.length).toBeGreaterThan(0)
    for (const reference of radiusReferences) {
      expect(reference.index).toBeGreaterThanOrEqual(layerOpeningEnd)
    }
    expect(out.code).not.toMatch(/@import\b[^;]*leaf-tokens\.css/i)
  }, 60_000)
})
