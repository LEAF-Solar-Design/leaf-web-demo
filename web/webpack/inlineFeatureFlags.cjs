const path = require('node:path')
const { parse } = require('@babel/parser')
const traverse = require('@babel/traverse').default
const generate = require('@babel/generator').default

// Inline only bindings imported from these exact source modules. Webpack can
// then remove guarded component dependencies before it creates its chunk graph.
const FLAGS = {
  'src/cadedit/flag.js': { ENV_CAD_EDIT: 'VITE_CAD_EDIT' },
  'src/projects/flag.js': { ENV_LIFECYCLE_UI: 'VITE_LIFECYCLE_UI' },
  'src/solar/flag.js': {
    ENV_SOLAR_SETTINGS_FORM: 'VITE_SOLAR_SETTINGS_FORM',
    ENV_SOLAR_FLOW_RAIL: 'VITE_SOLAR_FLOW_RAIL',
  },
}

module.exports = function inlineFeatureFlags(code, filename, { root, flags, inputMap } = {}) {
  if (!/ENV_(?:CAD_EDIT|LIFECYCLE_UI|SOLAR_SETTINGS_FORM|SOLAR_FLOW_RAIL)/.test(code)) return null
  const modules = new Map(Object.entries(FLAGS).map(([relative, names]) => [path.resolve(root, relative), names]))
  const ast = parse(code, { sourceType: 'module', plugins: ['jsx'] })
  let changed = false
  traverse(ast, {
    ImportDeclaration(importPath) {
      const source = importPath.node.source.value
      if (!source.startsWith('.')) return
      const names = modules.get(path.resolve(path.dirname(filename), source))
      if (!names) return
      let importChanged = false
      for (const specifier of importPath.get('specifiers')) {
        if (!specifier.isImportSpecifier()) continue
        const imported = specifier.node.imported.name ?? specifier.node.imported.value
        const key = names[imported]
        if (!key) continue
        if (typeof flags?.[key] !== 'boolean') throw new Error(`Missing compile-time boolean for ${key}`)
        const binding = importPath.scope.getBinding(specifier.node.local.name)
        if (!binding || binding.path.node !== specifier.node || !binding.constant) throw new Error(`Invalid feature flag binding: ${imported}`)
        for (const reference of binding.referencePaths) reference.replaceWith({ type: 'BooleanLiteral', value: flags[key] })
        specifier.remove()
        importChanged = true
        changed = true
      }
      if (importPath.node.specifiers.length === 0 && importChanged) importPath.remove()
    },
  })
  if (!changed) return null
  return generate(ast, {
    sourceMaps: true, inputSourceMap: inputMap,
    sourceFileName: path.relative(root, filename).split(path.sep).join('/'),
  }, code)
}
