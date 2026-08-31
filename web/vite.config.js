import { execSync } from 'node:child_process'
import { createReadStream, existsSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const HERE = path.dirname(fileURLToPath(import.meta.url))

// Card F-3: serve the compiled CAD engine to the dev server at /engine/*.
// The engine worker (spawned from the vendored boundary path) loads
// /engine/engine.js + /engine/engine_bg.wasm at runtime; in production those
// are staged into dist/engine by scripts/stage_cad_engine.mjs (Dockerfile
// step). In dev, this middleware maps them straight onto the wasm-pack
// --target web output. DISCOVERY NOTE, load-bearing: the vendor directory is
// found by shape (a vendor/* dir containing pkg-web), never spelled by name,
// because this file lives under web/ where the license fence forbids naming
// the engine crate outside the one legal Worker-spawn shape.
function cadEngineDevServer() {
  return {
    name: 'cad-engine-dev-server',
    configureServer(server) {
      const vendorRoot = path.resolve(HERE, '..', 'vendor')
      const pkgDir = existsSync(vendorRoot)
        ? readdirSync(vendorRoot)
          .map((name) => path.join(vendorRoot, name, 'pkg-web'))
          .find((candidate) => existsSync(candidate))
        : undefined
      server.middlewares.use('/engine', (req, res, next) => {
        if (!pkgDir) { next(); return }
        const wanted = (req.url || '').split('?')[0].replace(/^\//, '')
        const glue = readdirSync(pkgDir).find((f) => f.endsWith('_worker.js'))
        const wasm = readdirSync(pkgDir).find((f) => f.endsWith('_bg.wasm'))
        const file = wanted === 'engine.js' ? glue : wanted === 'engine_bg.wasm' ? wasm : null
        if (!file) { next(); return }
        res.setHeader('Content-Type', wanted.endsWith('.wasm') ? 'application/wasm' : 'text/javascript')
        createReadStream(path.join(pkgDir, file)).pipe(res)
      })
    },
  }
}

// Build marker: the short git sha of the checkout being built, so a stage
// tab can be proven fresh at a glance. Falls back to a timestamp when git
// isn't available (tarball copy, CI without .git).
function buildHash() {
  try {
    const sha = execSync('git rev-parse --short HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim()
    if (sha) return sha
  } catch { /* no git — fall through */ }
  return new Date().toISOString().slice(0, 16).replace('T', ' ')
}

// Lane C frontend. Dev server on 5175 so it never collides with the
// Lane D backend (8130). API base + mock mode are controlled via env
// (see src/api.js): VITE_MOCK=1 (default) demos with no backend.
export default defineConfig({
  plugins: [react(), cadEngineDevServer()],
  define: {
    __BUILD_HASH__: JSON.stringify(buildHash()),
  },
  server: { port: 5175, strictPort: false },
  build: {
    rollupOptions: {
      output: {
        // Split the heavy 3D + react-dom payload into its own vendor chunk so
        // first paint isn't blocked behind the viewer bundle.
        manualChunks(id) {
          if (!id.includes('node_modules')) return undefined
          if (/[\/]node_modules[\/](three|react-dom)([\/]|$)/.test(id)) return 'vendor-viewer'
          return undefined
        },
      },
    },
  },
})
