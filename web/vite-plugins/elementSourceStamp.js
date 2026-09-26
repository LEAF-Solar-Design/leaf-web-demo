import { createRequire } from 'node:module'
import { createHash } from 'node:crypto'
import { lstatSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

const require = createRequire(import.meta.url)
let parse
let generate
try {
  ;({ parse } = require('@babel/parser'))
  const generator = require('@babel/generator')
  generate = generator.default || generator
} catch (cause) {
  throw new Error('element-source-stamp requires @babel/parser and @babel/generator from the web install', { cause })
}

function componentName(parents) {
  for (let i = parents.length - 1, walked = 0; i >= 0 && walked < 32; i--, walked++) {
    const node = parents[i]
    if (node.type === 'FunctionDeclaration' || node.type === 'FunctionExpression') {
      if (node.id?.name) return node.id.name
    }
    if (node.type === 'ArrowFunctionExpression' || node.type === 'FunctionExpression') {
      const owner = parents[i - 1]
      if (owner?.type === 'VariableDeclarator' && owner.init === node && owner.id.type === 'Identifier') return owner.id.name
      if (owner?.type === 'AssignmentExpression' && owner.right === node && owner.left.type === 'Identifier') return owner.left.name
    }
  }
  return null
}

export const ELEMENT_REF_PATTERN = /^[0-9a-f]{12}$/

export function elementRef(stamp) {
  if (typeof stamp !== 'string' || !stamp.length || stamp.length > 200) {
    throw new TypeError('element-ref: stamp must be a non-empty string of at most 200 chars')
  }
  return createHash('sha256').update(stamp, 'utf8').digest('hex').slice(0, 12)
}

// Runs before React consumes JSX. Production registers refs only; the readable source stamp stays out of production (web/src/cadedit/elementSourceFence.test.js).
export default function elementSourceStamp({ root = process.cwd(), source = true, ref = true, onStamp } = {}) {
  if (!source && !ref) throw new TypeError('element-source-stamp: nothing to stamp')
  let webRoot = root.replace(/\\/g, '/')
  const stats = { stamped: 0, unnamed: 0 }
  return {
    name: 'element-source-stamp',
    enforce: 'pre',
    api: { source, ref },
    stats,
    configResolved(config) { webRoot = config.root.replace(/\\/g, '/') },
    transform(code, id) {
      if (!code.includes('data-element-id')) return null
      const filename = id.split('?')[0].replace(/\\/g, '/')
      const relative = path.posix.relative(webRoot, filename)
      if (!relative.startsWith('src/') || !relative.endsWith('.jsx') ||
          relative.split('/').includes('node_modules') || /\.(test|spec)\.jsx$/.test(relative) ||
          relative.includes(':')) return null

      const ast = parse(code, { sourceType: 'module', plugins: ['jsx'] })
      const pending = [{ node: ast, parents: [] }]
      let visited = 0
      let changed = false
      while (pending.length) {
        const { node, parents } = pending.pop()
        if (++visited > 1_000_000 || parents.length > 512) {
          throw new Error(`element-source-stamp AST walk limit exceeded: ${relative}`)
        }
        if (node.type === 'JSXOpeningElement') {
          const has = (name) => node.attributes.some((attr) => attr.type === 'JSXAttribute' && attr.name.name === name)
          if (has('data-element-id') && !has('data-element-source') && !has('data-element-ref')) {
            const name = componentName(parents)
            if (!name) stats.unnamed++
            else {
              const value = `${relative}:${name}`.slice(0, 200)
              if (ref) node.attributes.unshift({
                type: 'JSXAttribute',
                name: { type: 'JSXIdentifier', name: 'data-element-ref' },
                value: { type: 'StringLiteral', value: elementRef(value) },
              })
              if (source) node.attributes.unshift({
                type: 'JSXAttribute',
                name: { type: 'JSXIdentifier', name: 'data-element-source' },
                value: { type: 'StringLiteral', value },
              })
              onStamp?.(value)
              stats.stamped++
              changed = true
            }
          }
        }
        const ancestors = [...parents, node]
        for (const value of Object.values(node)) {
          for (const child of Array.isArray(value) ? value : [value]) {
            if (child && typeof child === 'object' && typeof child.type === 'string') {
              pending.push({ node: child, parents: ancestors })
            }
          }
        }
      }
      if (!changed) return null
      return generate(ast, { sourceMaps: true, sourceFileName: relative }, code)
    },
  }
}

export function resolveElementRefs(webRoot, refs, { maxFiles = 4000, maxBytes = 2_097_152 } = {}) {
  for (const ref of refs) {
    if (typeof ref !== 'string' || !ELEMENT_REF_PATTERN.test(ref)) {
      throw new TypeError(`element-ref: malformed ref ${String(ref)}`)
    }
  }
  webRoot = path.resolve(webRoot)
  const table = new Map()
  const pending = [path.join(webRoot, 'src')]
  let files = 0
  let skipped = 0
  while (pending.length) {
    const filename = pending.pop()
    const stat = lstatSync(filename)
    if (stat.isSymbolicLink()) continue
    if (stat.isDirectory()) {
      if (path.basename(filename) === 'node_modules') continue
      for (const name of readdirSync(filename).sort().reverse()) pending.push(path.join(filename, name))
      continue
    }
    if (!stat.isFile() || !filename.endsWith('.jsx') || /\.(test|spec)\.jsx$/.test(filename)) continue
    if (++files > maxFiles) throw new Error(`element-ref resolver: more than ${maxFiles} jsx files under src`)
    if (stat.size > maxBytes) { skipped++; continue }
    elementSourceStamp({
      root: webRoot, source: true, ref: false,
      onStamp(stamp) {
        const ref = elementRef(stamp)
        if (!table.has(ref)) table.set(ref, [])
        const stamps = table.get(ref)
        if (!stamps.includes(stamp)) stamps.push(stamp)
      },
    }).transform(readFileSync(filename, 'utf8'), filename)
  }
  for (const stamps of table.values()) stamps.sort()
  const matches = new Map(refs.map((ref) => [ref, table.get(ref) || []]))
  return { matches, table, skipped }
}
