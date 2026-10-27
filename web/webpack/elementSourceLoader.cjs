module.exports = function elementSourceLoader(code, inputMap) {
  const done = this.async()
  const options = this.getOptions()
  // The vendor worker uses Vite's ignore directive on a runtime engine URL.
  // Preserve that boundary in webpack without editing the licensed module.
  if (options.runtimeImportsOnly) {
    done(null, code.replace(/\/\*\s*@vite-ignore\s*\*\//g, '/* webpackIgnore: true */'), inputMap)
    return
  }
  import('../vite-plugins/elementSourceStamp.js').then(({ default: stamp }) => {
    if (!options.source && !options.ref) return done(null, code, inputMap)
    const result = stamp(options).transform(code, this.resourcePath)
    done(null, result?.code ?? code, result?.map ?? inputMap)
  }).catch(done)
}
