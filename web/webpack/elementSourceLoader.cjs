module.exports = function elementSourceLoader(code, inputMap) {
  const done = this.async()
  const options = this.getOptions()
  // The vendor worker uses Vite's ignore directive on a runtime engine URL.
  // Preserve that boundary without editing the licensed module. Next SWC
  // folds typeof window for client code, so worker scope uses a runtime lookup.
  if (options.runtimeImportsOnly) {
    done(null, code.replace(/\/\*\s*@vite-ignore\s*\*\//g, '/* webpackIgnore: true */')
      .replace(/typeof window === 'undefined'/g, "typeof globalThis.window === 'undefined'"), inputMap)
    return
  }
  import('../vite-plugins/elementSourceStamp.js').then(({ default: stamp }) => {
    // Source refs must be stamped before flag folding changes source structure.
    const stamped = options.source || options.ref ? stamp(options).transform(code, this.resourcePath) : null
    const stampedCode = stamped?.code ?? code
    const stampedMap = stamped?.map ?? inputMap
    const folded = options.featureFlags
      ? require('./inlineFeatureFlags.cjs')(stampedCode, this.resourcePath, {
        root: options.root, flags: options.featureFlags, inputMap: stampedMap,
      }) : null
    done(null, folded?.code ?? stampedCode, folded?.map ?? stampedMap)
  }).catch(done)
}
