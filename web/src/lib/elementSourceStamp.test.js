// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { parse } from '@babel/parser'
import { spawnSync } from 'node:child_process'
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import elementSourceStamp, { ELEMENT_REF_PATTERN, elementRef, resolveElementRefs } from '../../vite-plugins/elementSourceStamp.js'

const root = '/workspace/web'
const id = `${root}/src/site/SurfaceFrame.jsx`
const plugin = () => elementSourceStamp({ root })
const fixture = `
  const outside = <div data-element-id="outside" />;
  export function SurfaceFrame() {
    return <main data-element-id="frame"><span data-element-id="child" /></main>;
  }
  const Arrow = () => <aside data-element-id="arrow" />;
`

function stamps(code) {
  const values = []
  function walk(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'JSXAttribute' && node.name.name === 'data-element-source') values.push(node.value.value)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(parse(code, { sourceType: 'module', plugins: ['jsx'] }))
  return values
}

function refs(code) {
  const values = []
  function walk(node) {
    if (!node || typeof node !== 'object') return
    if (node.type === 'JSXAttribute' && node.name.name === 'data-element-ref') values.push(node.value.value)
    for (const value of Object.values(node)) {
      if (Array.isArray(value)) value.forEach(walk)
      else if (value && typeof value === 'object') walk(value)
    }
  }
  walk(parse(code, { sourceType: 'module', plugins: ['jsx'] }))
  return values
}

function withResolverFixture(run) {
  const fixtureRoot = mkdtempSync(join(tmpdir(), 'leaf-element-ref-'))
  try {
    mkdirSync(join(fixtureRoot, 'src', 'site'), { recursive: true })
    mkdirSync(join(fixtureRoot, 'src', 'node_modules'), { recursive: true })
    writeFileSync(join(fixtureRoot, 'src', 'site', 'SurfaceFrame.jsx'), fixture)
    for (const filename of ['Skip.test.jsx', 'Skip.spec.jsx', 'Skip.js']) {
      writeFileSync(join(fixtureRoot, 'src', 'site', filename), 'const Skip = () => <i data-element-id="skip" />')
    }
    writeFileSync(join(fixtureRoot, 'src', 'node_modules', 'Frame.jsx'), 'const Frame = () => <i data-element-id="dependency" />')
    return run(fixtureRoot)
  } finally {
    rmSync(fixtureRoot, { recursive: true, force: true })
  }
}

const resolverScript = fileURLToPath(new URL('../../scripts/resolve_element_ref.mjs', import.meta.url))
function runResolver(args, cwd = dirname(dirname(resolverScript))) {
  return spawnSync(process.execPath, [resolverScript, ...args], { cwd, encoding: 'utf8', timeout: 60_000 })
}

describe('element source stamp transform', () => {
  it('stamps an opaque twelve hex ref beside every source stamp', () => {
    const seen = []
    const instance = elementSourceStamp({ root, onStamp: (stamp) => seen.push(stamp) })
    const result = instance.transform(fixture, id)
    expect(refs(result.code)).toEqual(['1b582fc4fe99', '1b582fc4fe99', 'c85e7334dcc1'])
    expect(refs(result.code)).toEqual(stamps(result.code).map(elementRef))
    expect(seen.sort()).toEqual(stamps(result.code).sort())
    expect(instance.stats).toEqual({ stamped: 3, unnamed: 1 })
    expect(instance.api).toEqual({ source: true, ref: true })
    const spread = instance.transform('const Frame = () => <div data-element-id="x" {...props} />', id)
    const attributes = parse(spread.code, { sourceType: 'module', plugins: ['jsx'] }).program.body[0].declarations[0].init.body.openingElement.attributes
    expect(attributes.slice(0, 2).map((attr) => attr.name.name)).toEqual(['data-element-source', 'data-element-ref'])
    expect(attributes[3].type).toBe('JSXSpreadAttribute')
    expect(refs(spread.code)).toEqual(['6c0e0644b28d'])
  })

  it('ref only mode ships refs and no source text', () => {
    const instance = elementSourceStamp({ root, source: false })
    const result = instance.transform(fixture, id)
    expect(stamps(result.code)).toEqual([])
    expect(refs(result.code)).toEqual(['1b582fc4fe99', '1b582fc4fe99', 'c85e7334dcc1'])
    expect(result.code).not.toContain('data-element-source')
    expect(result.code).not.toContain('src/site')
    expect(instance.stats).toEqual({ stamped: 3, unnamed: 1 })
    const ast = parse(result.code, { sourceType: 'module', plugins: ['jsx'] })
    const pending = [ast]
    while (pending.length) {
      const node = pending.pop()
      if (node.type === 'JSXOpeningElement' && node.attributes.some((attr) => attr.name?.name === 'data-element-ref')) {
        expect(node.attributes[0].name.name).toBe('data-element-ref')
      }
      for (const value of Object.values(node)) {
        for (const child of Array.isArray(value) ? value : [value]) {
          if (child && typeof child === 'object' && typeof child.type === 'string') pending.push(child)
        }
      }
    }
    expect(instance.transform(result.code, id)).toBeNull()
  })

  it('refuses a stamp config with nothing to stamp', () => {
    expect(() => elementSourceStamp({ source: false, ref: false })).toThrow(new TypeError('element-source-stamp: nothing to stamp'))
  })

  it('leaves an element that already carries a ref untouched', () => {
    const code = 'const Frame = () => <div data-element-id="x" data-element-ref="abc" />'
    for (const options of [{}, { source: false }, { ref: false }]) {
      const instance = elementSourceStamp({ root, ...options })
      expect(instance.transform(code, id)).toBeNull()
      expect(instance.stats.stamped).toBe(0)
    }
  })

  it('elementRef is the first twelve hex of the sha256 of the stamp and refuses malformed input', () => {
    const cases = [
      ['src/site/SurfaceFrame.jsx:SurfaceFrame', '1b582fc4fe99'],
      ['src/site/SurfaceFrame.jsx:Arrow', 'c85e7334dcc1'],
      ['src/site/SurfaceFrame.jsx:Frame', '6c0e0644b28d'],
      ['src/site/SurfaceFrame.jsx:Inner', '7698bb3d9f04'],
      ['src/site/Frame.jsx:Frame', '279230653988'],
      [('src/site/' + 'x'.repeat(220) + '.jsx:Frame').slice(0, 200), '14bb8f9ee66d'],
      ['src/site/CockpitTopBand.jsx:QuickButton', '002e75b5da50'],
    ]
    for (const [stamp, ref] of cases) {
      expect(elementRef(stamp)).toBe(ref)
      expect(ref).toMatch(ELEMENT_REF_PATTERN)
    }
    for (const invalid of ['', 'x'.repeat(201), 7, null, undefined]) {
      expect(() => elementRef(invalid)).toThrow(new TypeError('element-ref: stamp must be a non-empty string of at most 200 chars'))
    }
  })

  it('vite config registers refs only in production mode', async () => {
    const previous = process.env.LEAF_ELEMENT_REF_STAMP
    try {
      const { default: config } = await import('../../vite.config.js')
      for (const value of [undefined, '', '1']) {
        if (value === undefined) delete process.env.LEAF_ELEMENT_REF_STAMP
        else process.env.LEAF_ELEMENT_REF_STAMP = value
        const production = config({ mode: 'production', command: 'build' }).plugins.flat(Infinity).find((entry) => entry.name === 'element-source-stamp')
        expect(production.api).toEqual({ source: false, ref: true })
      }
      delete process.env.LEAF_ELEMENT_REF_STAMP
      const staging = config({ mode: 'staging', command: 'build' }).plugins.flat(Infinity).find((entry) => entry.name === 'element-source-stamp')
      expect(staging.api).toEqual({ source: true, ref: true })
    } finally {
      if (previous === undefined) delete process.env.LEAF_ELEMENT_REF_STAMP
      else process.env.LEAF_ELEMENT_REF_STAMP = previous
    }
  }, 60_000)

  it('the LEAF_ELEMENT_REF_STAMP kill switch drops refs and a malformed value fails the config', async () => {
    const previous = process.env.LEAF_ELEMENT_REF_STAMP
    try {
      const { default: config } = await import('../../vite.config.js')
      process.env.LEAF_ELEMENT_REF_STAMP = '0'
      const production = config({ mode: 'production', command: 'build' }).plugins.flat(Infinity).find((entry) => entry.name === 'element-source-stamp')
      expect(production).toBeUndefined()
      const staging = config({ mode: 'staging', command: 'build' }).plugins.flat(Infinity).find((entry) => entry.name === 'element-source-stamp')
      expect(staging.api).toEqual({ source: true, ref: false })
      expect(refs(staging.transform(fixture, `${dirname(dirname(resolverScript))}/src/site/SurfaceFrame.jsx`).code)).toEqual([])
      for (const mode of ['production', 'staging']) {
        process.env.LEAF_ELEMENT_REF_STAMP = 'yes'
        expect(() => config({ mode, command: 'build' })).toThrow('LEAF_ELEMENT_REF_STAMP must be 0 or 1')
      }
    } finally {
      if (previous === undefined) delete process.env.LEAF_ELEMENT_REF_STAMP
      else process.env.LEAF_ELEMENT_REF_STAMP = previous
    }
  }, 60_000)

  it('resolveElementRefs maps refs back to stamps and skips tests and dependencies', () => {
    withResolverFixture((fixtureRoot) => {
      const { matches, table, skipped } = resolveElementRefs(fixtureRoot, ['1b582fc4fe99', 'c85e7334dcc1', 'ffffffffffff'])
      expect(Object.fromEntries(matches)).toEqual({
        '1b582fc4fe99': ['src/site/SurfaceFrame.jsx:SurfaceFrame'],
        c85e7334dcc1: ['src/site/SurfaceFrame.jsx:Arrow'],
        ffffffffffff: [],
      })
      expect(table.size).toBe(2)
      expect(skipped).toBe(0)
      expect(() => resolveElementRefs(fixtureRoot, ['XYZ'])).toThrow(TypeError)
      expect(() => resolveElementRefs(fixtureRoot, [], { maxFiles: 0 })).toThrow('more than 0 jsx files under src')
      const oversized = resolveElementRefs(fixtureRoot, ['1b582fc4fe99'], { maxBytes: 1 })
      expect(oversized.skipped).toBe(1)
      expect(oversized.table.size).toBe(0)
      expect(oversized.matches.get('1b582fc4fe99')).toEqual([])
    })
  })

  it('the resolver CLI prints refs, lists the table and exits by contract', () => {
    withResolverFixture((fixtureRoot) => {
      const run = (...args) => runResolver(['--root', fixtureRoot, ...args])
      const mixed = run('1b582fc4fe99', 'ffffffffffff')
      expect(mixed.error).toBeUndefined()
      expect(mixed.status).toBe(1)
      expect(mixed.stdout).toBe('1b582fc4fe99\tsrc/site/SurfaceFrame.jsx:SurfaceFrame\nffffffffffff\tUNRESOLVED\n')
      const resolved = run('1b582fc4fe99')
      expect(resolved.status).toBe(0)
      expect(resolved.stdout).toBe('1b582fc4fe99\tsrc/site/SurfaceFrame.jsx:SurfaceFrame\n')
      const listed = run('--list')
      expect(listed.status).toBe(0)
      expect(listed.stdout).toBe('c85e7334dcc1\tsrc/site/SurfaceFrame.jsx:Arrow\n1b582fc4fe99\tsrc/site/SurfaceFrame.jsx:SurfaceFrame\n')
      const malformed = run('nothex')
      expect(malformed.status).toBe(2)
      expect(malformed.stderr).toContain('malformed ref')
      for (const args of [[], ['--unknown'], ['--root'], Array(257).fill('1b582fc4fe99')]) {
        const result = run(...args)
        expect(result.status).toBe(2)
        expect(result.stderr.length).toBeGreaterThan(0)
      }
    })
  }, 60_000)

  it('the resolver CLI resolves a real stamp in this tree', () => {
    const result = runResolver(['002e75b5da50'])
    expect(result.error).toBeUndefined()
    expect(result.status).toBe(0)
    expect(result.stderr).toBe('')
    expect(result.stdout).toBe('002e75b5da50\tsrc/site/CockpitTopBand.jsx:QuickButton\n')
  }, 60_000)

  it('stamps each identified element with its nearest named component', () => {
    const instance = plugin()
    const result = instance.transform(fixture, id)
    expect(stamps(result.code)).toEqual([
      'src/site/SurfaceFrame.jsx:SurfaceFrame',
      'src/site/SurfaceFrame.jsx:SurfaceFrame',
      'src/site/SurfaceFrame.jsx:Arrow',
    ])
    expect(instance.stats.stamped).toBe(3)
  })

  it('counts and leaves an element outside a named component unstamped', () => {
    const instance = plugin()
    expect(instance.transform('const x = <div data-element-id="x" />', id)).toBeNull()
    expect(instance.stats.unnamed).toBe(1)
  })

  it('leaves files without an identity byte-identical', () => {
    const code = 'export const Frame = () => <main  title="hello" />\n'
    const result = plugin().transform(code, id)
    expect(result?.code ?? code).toBe(code)
    expect(result).toBeNull()
  })

  it('is idempotent and preserves existing stamps', () => {
    const instance = plugin()
    const first = instance.transform(fixture, id).code
    expect(instance.transform(first, id)).toBeNull()
    expect(instance.stats.stamped).toBe(3)
    expect(instance.transform('const X = () => <div data-element-id="x" data-element-source="kept" />', id)).toBeNull()
  })

  it('emits the generated stamp first so spread values win when supplied', () => {
    const code = 'const Frame = () => <div data-element-id="x" {...props} />'
    const result = plugin().transform(code, id)
    expect(result.code.indexOf('data-element-source')).toBeLessThan(result.code.indexOf('{...props}'))
    const ast = parse(result.code, { sourceType: 'module', plugins: ['jsx'] })
    const attributes = ast.program.body[0].declarations[0].init.body.openingElement.attributes
    expect(attributes[0].name.name).toBe('data-element-source')
    const evaluate = (props) => attributes.reduce((merged, attr) => {
      if (attr.type === 'JSXSpreadAttribute') {
        expect(attr.argument.name).toBe('props')
        return { ...merged, ...props }
      }
      return { ...merged, [attr.name.name]: attr.value.value }
    }, {})
    expect(evaluate({ 'data-element-source': 'spread:Value' })['data-element-source']).toBe('spread:Value')
    expect(evaluate({})['data-element-source']).toBe('src/site/SurfaceFrame.jsx:Frame')
  })

  it('leaves an explicit literal stamp untouched without adding a second stamp', () => {
    const code = 'const Frame = () => <div data-element-id="x" data-element-source="x:Y" />'
    const result = plugin().transform(code, id)
    expect(result).toBeNull()
    expect(result?.code ?? code).toBe(code)
    expect(stamps(result?.code ?? code)).toEqual(['x:Y'])
  })

  it('adds exactly one stamp to an element with a class name', () => {
    const code = 'const Frame = () => <div data-element-id="x" className="a" />'
    const result = plugin().transform(code, id)
    expect(stamps(result.code)).toEqual(['src/site/SurfaceFrame.jsx:Frame'])
    expect(result.code).toContain('className="a"')
  })

  it('refuses JavaScript, tests, dependencies and files outside src', () => {
    for (const filename of ['src/site/Frame.js', 'src/Frame.test.jsx', 'src/Frame.spec.jsx', 'node_modules/Frame.jsx', 'src/node_modules/Frame.jsx', 'other/Frame.jsx']) {
      expect(plugin().transform(fixture, `${root}/${filename}`)).toBeNull()
    }
  })

  it('normalizes Windows paths and bounds the stamp length', () => {
    const instance = elementSourceStamp({ root: 'C:\\workspace\\web' })
    const code = 'const Frame = () => <div data-element-id="x" />'
    expect(stamps(instance.transform(code, 'C:\\workspace\\web\\src\\site\\Frame.jsx').code)).toEqual(['src/site/Frame.jsx:Frame'])
    const [stamp] = stamps(plugin().transform(code, `${root}/src/site/${'x'.repeat(220)}.jsx`).code)
    expect(stamp.length).toBe(200)
    expect(stamp).not.toMatch(/\\|^[A-Za-z]:|^\//)
  })

  it('uses the nearest name through anonymous callbacks and stops after 32 parents', () => {
    const code = 'function Outer() { const Inner = () => items.map(x => <div data-element-id="x" />); return Inner }'
    expect(stamps(plugin().transform(code, id).code)).toEqual(['src/site/SurfaceFrame.jsx:Inner'])
    const deep = `function Deep() { return ${'<div>'.repeat(40)}<i data-element-id="x" />${'</div>'.repeat(40)} }`
    const instance = plugin()
    expect(instance.transform(deep, id)).toBeNull()
    expect(instance.stats.unnamed).toBe(1)
  })
})
