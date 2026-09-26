// @vitest-environment node
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { transformSync } from 'esbuild'
import { expect, test } from 'vitest'

function productionModules(directory) {
  const modules = []
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) {
      if (entry.name !== '__mocks__') modules.push(...productionModules(path))
    } else if (entry.isFile() && /\.(js|jsx|mjs)$/.test(entry.name)
      && !/\.(test|spec)\.(js|jsx|mjs)$/.test(entry.name)) {
      modules.push({
        path: relative(process.cwd(), path).split(sep).join('/'),
        source: readFileSync(path, 'utf8'),
      })
    }
  }
  return modules
}

function transformedEngineSource(source) {
  if (!source.includes('EngineSessionProvider') && !source.includes('EngineBoundary')) return ''
  return transformSync(source, { loader: 'jsx', jsx: 'transform' }).code
}

function countProviderMounts(source) {
  return (transformedEngineSource(source).match(/React\.createElement\(\s*EngineSessionProvider\b/g) || []).length
}

const modules = productionModules(resolve(process.cwd(), 'src'))

test('exactly one production module mounts EngineSessionProvider and it is App.jsx', () => {
  const mounts = modules.map(({ path, source }) => ({ path, count: countProviderMounts(source) }))
  expect(mounts.reduce((total, { count }) => total + count, 0)).toBe(1)
  expect(mounts.filter(({ count }) => count > 0).map(({ path }) => path)).toEqual(['src/App.jsx'])
})

test('only App.jsx default-imports EngineSessionProvider among production modules', () => {
  const importers = modules.filter(({ source }) => /^import\s+EngineSessionProvider\b/m.test(source))
  expect(importers.map(({ path }) => path)).toEqual(['src/App.jsx'])
})

test('EngineBoundary is constructed only in cadedit/engineSession.js', () => {
  const constructors = modules.filter(({ source }) => /new\s+EngineBoundary\s*\(/.test(transformedEngineSource(source)))
  expect(constructors.map(({ path }) => path)).toEqual(['src/cadedit/engineSession.js'])
})

test('the mount counter reports a second mount when one is added', () => {
  const source = readFileSync(resolve(process.cwd(), 'src', 'App.jsx'), 'utf8')
  const secondMount = `
export function AdditionalEngineMountForTest() {
  return <EngineSessionProvider>{null}</EngineSessionProvider>
}
`
  expect(countProviderMounts(source + secondMount)).toBe(2)
})
