import { createRequire } from 'node:module'
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

// Runs before React consumes JSX. Production excludes this plugin at registration.
export default function elementSourceStamp({ root = process.cwd() } = {}) {
  let webRoot = root.replace(/\\/g, '/')
  const stats = { stamped: 0, unnamed: 0 }
  return {
    name: 'element-source-stamp',
    enforce: 'pre',
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
          if (has('data-element-id') && !has('data-element-source')) {
            const name = componentName(parents)
            if (!name) stats.unnamed++
            else {
              node.attributes.push({
                type: 'JSXAttribute',
                name: { type: 'JSXIdentifier', name: 'data-element-source' },
                value: { type: 'StringLiteral', value: `${relative}:${name}`.slice(0, 200) },
              })
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
