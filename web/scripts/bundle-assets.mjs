// Read the artifact that is deployed. These helpers never invoke a compiler.
import { readFileSync, readdirSync, realpathSync, statSync } from 'node:fs'
import { parse } from '@babel/parser'
import { basename, dirname, relative, resolve, sep } from 'node:path'

export function emittedAssets(directory) {
  const root = realpathSync(directory)
  const assets = new Map()
  function walk(folder) {
    for (const entry of readdirSync(folder, { withFileTypes: true })) {
      const file = resolve(folder, entry.name)
      if (entry.isSymbolicLink()) throw new Error(`Symbolic link in bundle: ${file}`)
      if (entry.isDirectory()) walk(file)
      else if (/\.(?:js|css)$/.test(entry.name)) {
        assets.set(relative(root, file).split(sep).join('/'), readFileSync(file, 'utf8'))
      }
    }
  }
  walk(root)
  if (!assets.size) throw new Error(`No emitted JS/CSS in ${directory}`)
  return assets
}

export function scriptPaths(html) {
  const paths = []
  // Script bodies are skipped so inline RSC data cannot impersonate a tag.
  for (const tag of html.matchAll(/<script\b([^>]*)>[\s\S]*?<\/script\s*>/gi)) {
    const sources = [...tag[1].matchAll(/(?:^|\s)src\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi)]
    if (!sources.length) continue
    const source = sources[0][1] ?? sources[0][2] ?? sources[0][3]
    const file = source.replace(/%5b/gi, '[').replace(/%5d/gi, ']').replace(/^\//, '')
    if (sources.length !== 1 || !/^(?:assets\/[A-Za-z0-9_-]+\.js|_next\/static\/(?:[A-Za-z0-9_@.\[\]-]+\/)*[A-Za-z0-9_@.\[\]-]+\.js)$/.test(file) ||
        file.split('/').some(part => part === '.' || part === '..')) {
      throw new Error(`Unsafe script source: ${source}`)
    }
    if (!paths.includes(file)) paths.push(file)
  }
  if (!paths.length) throw new Error('No local script elements in index.html')
  return paths
}

export function bundleGraph(directory) {
  const assets = emittedAssets(directory)
  const html = readFileSync(resolve(directory, 'index.html'), 'utf8')
  const entries = scriptPaths(html)
  for (const file of entries) if (!assets.has(file)) throw new Error(`Missing entry script: ${file}`)
  // Webpack records the emitted chunk IDs in the chunk registration itself.
  // Resolve .e(id) lazy imports and .u(id) worker URLs against those bytes,
  // rather than assuming a stable filename or trusting a producer receipt.
  const ids = new Map()
  const modules = new Map()
  for (const [file, code] of assets) {
    for (const match of code.matchAll(/(?:\{|,)(\d+):(?:function\s*\(|\([^)]*\)\s*=>)/g)) {
      if (!modules.has(match[1])) modules.set(match[1], new Set())
      modules.get(match[1]).add(file)
    }
    const workerId = basename(file).match(/^(\d+)\.[A-Za-z0-9_-]+\.js$/)?.[1]
    if (workerId) {
      if (!ids.has(workerId)) ids.set(workerId, new Set())
      ids.get(workerId).add(file)
    }
    for (const match of code.matchAll(/\.push\(\[\[([\d,\s]+)\]/g)) {
      for (const id of match[1].split(',').map(value => value.trim()).filter(Boolean)) {
        if (!ids.has(id)) ids.set(id, new Set())
        ids.get(id).add(file)
      }
    }
  }
  // Webpack .e(id) also loads CSS-only chunks. Read the runtime's emitted
  // miniCssF table and require its concrete file, rather than treating every
  // lazy dependency as JavaScript or silently accepting unknown IDs.
  for (const [, code] of assets) {
    for (const table of code.matchAll(/\.miniCssF=\w+=>"static\/css\/"\+\(\{([^}]+)\}\)\[\w+\]\+"\.css"/g)) {
      for (const entry of table[1].matchAll(/(\d+):"([a-f0-9]+)"/g)) {
        const file = `_next/static/css/${entry[2]}.css`
        if (!assets.has(file)) throw new Error(`Missing CSS chunk: ${file}`)
        if (!ids.has(entry[1])) ids.set(entry[1], new Set())
        ids.get(entry[1]).add(file)
      }
    }
  }
  const dependencies = new Map()
  const staticDependencies = new Map()
  for (const [file, code] of assets) {
    const direct = new Set()
    const eager = new Set()
    const requireNames = new Set([...code.matchAll(/(?:\{|,)\d+:(?:function\s*)?\(\s*[\w$]+\s*,\s*[\w$]+\s*,\s*([\w$]+)\s*\)/g)].map(match => match[1]))
    for (const match of code.matchAll(/\b([\w$]+)\(\s*(\d+)\s*\)/g)) {
      if (!requireNames.has(match[1])) continue
      for (const target of modules.get(match[2]) || []) if (target !== file) {
        direct.add(target)
        eager.add(target)
      }
    }
    // Parse executable imports instead of matching import-like prose inside
    // dependency error messages (XYFlow includes such examples in strings).
    if (file.endsWith('.js')) {
      const tree = parse(code, { sourceType: 'unambiguous' })
      const queue = [tree.program]
      for (const node of queue) {
        let source
        if (['ImportDeclaration', 'ExportNamedDeclaration', 'ExportAllDeclaration'].includes(node.type)) source = node.source
        else if (node.type === 'ImportExpression') source = node.source
        else if (node.type === 'CallExpression' && node.callee.type === 'Import') source = node.arguments[0]
        else if (node.type === 'NewExpression' && node.callee.name === 'URL') source = node.arguments[0]
        if (source?.type === 'StringLiteral' && /\.(js|css)$/.test(source.value)) {
          const value = source.value
          const target = value.startsWith('/') ? value.slice(1) : relative(resolve(directory), resolve(directory, dirname(file), value)).split(sep).join('/')
          if (!assets.has(target)) throw new Error(`Missing dependency ${value} from ${file}`)
          direct.add(target)
          eager.add(target)
        }
        for (const [key, child] of Object.entries(node)) {
          if (['loc', 'start', 'end', 'extra', 'comments', 'tokens'].includes(key)) continue
          if (Array.isArray(child)) { for (const item of child) if (item?.type) queue.push(item) }
          else if (child?.type) queue.push(child)
        }
      }
    }
    for (const match of code.matchAll(/\.(?:e|u)\(\s*(\d+)\s*\)/g)) {
      const targets = ids.get(match[1])
      if (!targets) throw new Error(`Unresolved webpack chunk ${match[1]} from ${file}`)
      for (const target of targets) direct.add(target)
    }
    // Vite rollback output uses literal import URLs; webpack worker chunks
    // can also have literal URLs. Only complete quoted references count.
    for (const [target] of assets) {
      if (target === file) continue
      const name = basename(target)
      if ([`"${target}"`, `'${target}'`, `"/${target}"`, `'/${target}'`, `"./${name}"`, `'./${name}'`].some(value => code.includes(value))) direct.add(target)
    }
    dependencies.set(file, direct)
    staticDependencies.set(file, eager)
  }
  function closure(roots = entries, edges = dependencies) {
    const visited = new Set()
    const queue = [...roots]
    for (const file of queue) {
      if (visited.has(file)) continue
      if (!assets.has(file)) throw new Error(`Missing bundle asset: ${file}`)
      visited.add(file)
      queue.push(...(edges.get(file) || []))
    }
    return visited
  }
  const styles = []
  for (const tag of html.matchAll(/<link\b[^>]*>/gi)) {
    if (!/\brel\s*=\s*["']stylesheet["']/i.test(tag[0])) continue
    const href = tag[0].match(/\bhref\s*=\s*["']([^"']+)["']/i)?.[1]
    if (!href || !/^\/?(?:_next\/static|assets)\/[A-Za-z0-9_@./-]+\.css$/.test(href) || href.split('/').includes('..')) throw new Error(`Unsafe stylesheet: ${href}`)
    styles.push(href.replace(/^\//, ''))
  }
  return { assets, entries, dependencies, staticDependencies, closure, reachable: closure(), startup: closure([...entries, ...styles], staticDependencies) }
}

export function emittedJavaScript(directory) {
  return new Map([...bundleGraph(directory).assets].filter(([file]) => file.endsWith('.js')))
}

export function fenceDirectory(label) {
  const key = `LEAF_BUNDLE_${label.toUpperCase().replaceAll('-', '_')}_DIR`
  const directory = process.env[key]
  if (!directory) throw new Error(`Build the Next fixture first and set ${key}; bundle tests never run a second compiler`)
  const root = resolve(directory)
  if (!statSync(resolve(root, '_next/static')).isDirectory()) throw new Error(`Not a Next static export: ${root}`)
  bundleGraph(root)
  return root
}
