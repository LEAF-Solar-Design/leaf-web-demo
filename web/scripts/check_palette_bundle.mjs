import { readFileSync, statSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Follow only static imports. The palette itself is a dynamic root, while
// App and the lazy viewers must remain outside its initial load.
const dist = resolve(dirname(fileURLToPath(import.meta.url)), '../dist')

try {
  const manifest = JSON.parse(readFileSync(resolve(dist, '.vite/manifest.json'), 'utf8'))
  const entries = Object.keys(manifest).filter((key) => manifest[key].isEntry)
  const palettes = Object.keys(manifest).filter((key) =>
    /(?:^|\/)LeafPlatformScene\.jsx$/.test(manifest[key].src || key))
  if (entries.length === 0) throw new Error('No entry chunk found in the build manifest')
  if (palettes.length !== 1) throw new Error(`Expected one LeafPlatformScene chunk, found ${palettes.length}`)

  const visited = new Set()
  const files = new Set()
  const offending = []
  const queue = [...entries, ...palettes].map((key) => [key])
  for (let index = 0; index < queue.length; index += 1) {
    const chain = queue[index]
    const key = chain[chain.length - 1]
    if (visited.has(key)) continue
    visited.add(key)
    const chunk = manifest[key]
    if (!chunk?.file) throw new Error(`Missing chunk in static import chain: ${chain.join(' -> ')}`)
    files.add(chunk.file)
    for (const css of chunk.css || []) files.add(css)
    const code = readFileSync(resolve(dist, chunk.file), 'utf8')
    // Names catch the configured vendor chunk; runtime signatures also catch
    // three.js in a renamed or merged chunk without matching UI prose.
    if (/vendor-viewer|three/i.test([key, chunk.file, chunk.name, chunk.src].join(' ')) ||
        /\bTHREE\b|__THREE__|three\.(?:js|module|core)|node_modules[/\\]three[/\\]/.test(code)) {
      offending.push(chain.map((part) => `${part} (${manifest[part].file})`).join(' -> '))
    }
    for (const dependency of chunk.imports || []) queue.push([...chain, dependency])
  }

  const bytes = [...files].reduce((total, file) => total + statSync(resolve(dist, file)).size, 0)
  console.log(`Palette static closure: ${visited.size} chunks, ${files.size} JS/CSS files, ${bytes} bytes`)
  if (offending.length) {
    for (const chain of offending) console.error(`Viewer dependency in palette initial load: ${chain}`)
    process.exitCode = 1
  } else {
    console.log('Palette bundle check passed: no viewer or three.js in the static closure')
  }
} catch (error) {
  console.error(`Palette bundle check failed: ${error.message}`)
  process.exitCode = 1
}
