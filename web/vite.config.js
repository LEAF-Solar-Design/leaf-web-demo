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
      // Managed local proofs compile the same wasm-pack output as production
      // into their isolated run directory. Normal developer runs keep the
      // shape-based vendor discovery below, so no generated engine bytes need
      // to be written into or retained by the checkout.
      const managedPkgDir = process.env.LEAF_CAD_ENGINE_PKG_DIR
      const pkgDir = managedPkgDir
        ? path.resolve(managedPkgDir)
        : existsSync(vendorRoot)
          ? readdirSync(vendorRoot)
            .map((name) => path.join(vendorRoot, name, 'pkg-web'))
            .find((candidate) => existsSync(candidate))
          : undefined
      server.middlewares.use('/engine', (req, res, next) => {
        if (!pkgDir) { next(); return }
        // The pkg is built with --out-name engine (see the Dockerfile's
        // engine stage), so the served names ARE the on-disk names — no
        // rename mapping, because the wasm's import module key is baked to
        // the build-time name and a mapped alias cannot change it.
        const wanted = (req.url || '').split('?')[0].replace(/^\//, '')
        if (wanted !== 'engine.js' && wanted !== 'engine_bg.wasm') { next(); return }
        if (!existsSync(path.join(pkgDir, wanted))) { next(); return }
        res.setHeader('Content-Type', wanted.endsWith('.wasm') ? 'application/wasm' : 'text/javascript')
        createReadStream(path.join(pkgDir, wanted)).pipe(res)
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
  server: {
    port: 5175,
    strictPort: false,
    // The editing surface's engine worker lives under the repo's vendor/
    // (spawned via the one fence-legal URL shape); vite's dev-server fs
    // sandbox must therefore admit that tree or the worker 403s in dev.
    fs: { allow: [HERE, path.resolve(HERE, '..', 'vendor')] },
  },
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
