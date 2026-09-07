// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { parse } from '@babel/parser'
import elementSourceStamp from '../../vite-plugins/elementSourceStamp.js'

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

describe('element source stamp transform', () => {
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
