// @vitest-environment node
import { scaleTimeout } from '../testTimeScale.js'
import { readFileSync, readdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { preprocessCSS, resolveConfig } from 'vite'

const webDir = fileURLToPath(new URL('../../', import.meta.url))
const srcDir = fileURLToPath(new URL('../', import.meta.url))
const labPath = fileURLToPath(new URL('./lab.css', import.meta.url))
const sourcesDir = fileURLToPath(new URL('./contract/sources', import.meta.url))
const labCSS = readFileSync(labPath, 'utf8')
const wrapperPath = join(dirname(labPath), 'labTokensLayer.css')
const tokenBytes = readFileSync(join(sourcesDir, 'leaf-tokens.css'))
const require = createRequire(import.meta.url)
const postcss = require('postcss')
const importParser = require('next/dist/build/webpack/loaders/css-loader/src/plugins/postcss-import-parser').default
const cssRuntime = require('next/dist/build/webpack/loaders/css-loader/src/runtime/api')
const stripComments = source => source.replace(/\/\*[\s\S]*?\*\//g, '')

// Use the production parser's import records and the production runtime's media handling.
async function nextImports(source, path) {
  const imports = []
  const api = []
  const result = await postcss([importParser({
    imports,
    api,
    context: dirname(path),
    rootContext: webDir,
    resourcePath: path,
    resolver: async (context, request) => resolve(context, request),
    urlHandler: url => url,
  })]).process(source, { from: path, map: false })
  expect(result.warnings()).toEqual([])
  const runtime = cssRuntime(false)
  for (const entry of api) {
    const imported = imports.find(item => item.importName === entry.importName)
    const child = await nextImports(readFileSync(imported.url, 'utf8'), imported.url)
    runtime.i(child.runtime, entry.media)
  }
  runtime.push([path, result.css, ''])
  return { api, imports, runtime, css: runtime.toString() }
}

const tokenRules = root => {
  const rules = []
  root.walkRules(rule => rules.push({
    selector: rule.selector,
    declarations: rule.nodes.map(node => [node.prop, node.value, Boolean(node.important)]),
  }))
  return rules
}

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
  it('imports the generated native layer as its first statement', () => {
    const firstStatement = stripComments(labCSS).trim().match(/^[^;]*;/)?.[0]
    expect(firstStatement).toBe("@import './labTokensLayer.css';")
  })

  it('keeps application sources from referencing the vendored token stylesheet directly', () => {
    const references = sourceFiles(srcDir)
      .filter(path => !/\.test\.[cm]?[jt]sx?$/.test(path))
      .filter(path => readFileSync(path, 'utf8').includes('leaf-tokens.css'))
      .map(path => relative(srcDir, path))
    expect(references).toEqual([])
  })

  it('checks the generated wrapper against the exact vendored bytes', () => {
    expect(readFileSync(wrapperPath).equals(Buffer.concat([
      Buffer.from('@layer leaf-lab-tokens {\n', 'utf8'),
      tokenBytes,
      Buffer.from('\n}\n', 'utf8'),
    ]))).toBe(true)
  })

  it('reproduces the original layered-import media misparse with the Next producer', async () => {
    const broken = await nextImports(
      "@import './contract/sources/leaf-tokens.css' layer(leaf-lab-tokens);", labPath,
    )
    expect(broken.api).toHaveLength(1)
    expect(broken.api[0].media).toBe('layer(leaf-lab-tokens)')
    const root = postcss.parse(broken.css)
    expect(root.nodes[0].name).toBe('media')
    expect(root.nodes[0].params).toBe('layer(leaf-lab-tokens)')
    expect(tokenRules(root)).toEqual(tokenRules(postcss.parse(tokenBytes.toString('utf8'))))
  })

  it.each([
    ['base', ':root', 'initial', ' '],
    ['light', '[data-leaf-theme="light"]', 'initial', ' '],
    ['dark', '.dark, [data-leaf-theme="dark"]', ' ', 'initial'],
  ])('preserves all %s tokens through the ordinary Next CSS import', async (_variant, selector, dark, light) => {
    const output = await nextImports(labCSS, labPath)
    expect(output.imports.map(entry => entry.url)).toEqual([
      wrapperPath, join(dirname(labPath), 'studioParityAdapter.css'),
    ])
    expect(output.api).toHaveLength(2)
    for (const entry of output.api) expect(entry.media).toBeUndefined()
    const root = postcss.parse(output.css)
    const layers = root.nodes.filter(node => node.type === 'atrule' && node.name === 'layer')
    expect(layers).toHaveLength(1)
    const layer = layers[0]
    expect(layer.params).toBe('leaf-lab-tokens')
    const originalRules = tokenRules(postcss.parse(tokenBytes.toString('utf8')))
    expect(tokenRules(layer)).toEqual(originalRules)
    const theme = layer.nodes.find(node => node.type === 'rule' && node.selector === selector)
    expect(theme).toBeDefined()
    expect(theme.nodes.find(node => node.prop === '--leaf-when-dark').value).toBe(dark)
    expect(theme.nodes.find(node => node.prop === '--leaf-when-light').value).toBe(light)
    for (const profile of ['mushy', 'ops']) {
      expect(tokenRules(layer).some(rule => rule.selector.split(',')
        .some(part => part.trim() === `[data-leaf-profile="${profile}"]`))).toBe(true)
    }
    root.walkAtRules('media', rule => expect(rule.params).not.toContain('layer('))
    root.walkDecls(/^--leaf-/, declaration => {
      if (declaration.parent.selector?.includes('.lab-workbench')) return
      expect(declaration.parent.parent).toBe(layer)
    })
  })

  it('keeps the shared shell radius unlayered when Lab CSS loads lazily', async () => {
    const shellPath = join(srcDir, 'standard/leaf-standard.css')
    const shell = await nextImports(readFileSync(shellPath, 'utf8'), shellPath)
    const lab = await nextImports(labCSS, labPath)
    for (const order of [[shell, lab], [lab, shell]]) {
      const runtime = cssRuntime(false)
      for (const output of order) runtime.i(output.runtime)
      const root = postcss.parse(runtime.toString())
      const unlayeredRadii = []
      root.walkDecls('--leaf-radius-control', declaration => {
        if (declaration.parent.parent.type === 'root') unlayeredRadii.push(declaration.value)
        else {
          expect(declaration.parent.parent.name).toBe('layer')
          expect(declaration.parent.parent.params).toBe('leaf-lab-tokens')
        }
      })
      expect(unlayeredRadii).toEqual(['6px'])
    }
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
  }, scaleTimeout(60_000))
})
