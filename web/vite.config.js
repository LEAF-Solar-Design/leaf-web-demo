import { execSync } from 'node:child_process'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Build marker: an immutable source SHA. CLI cloud deploys do not upload .git,
// so deploy-web.py supplies LEAF_SOURCE_SHA explicitly. Never substitute a
// timestamp: that makes source-to-deployment attribution impossible.
function buildHash() {
  const injected = process.env.LEAF_SOURCE_SHA || process.env.VERCEL_GIT_COMMIT_SHA || ''
  if (/^[0-9a-f]{40}$/i.test(injected)) return injected.toLowerCase()
  try {
    const sha = execSync('git rev-parse HEAD', { stdio: ['ignore', 'pipe', 'ignore'] })
      .toString().trim()
    if (/^[0-9a-f]{40}$/i.test(sha)) return sha.toLowerCase()
  } catch { /* handled below */ }
  throw new Error('immutable build SHA unavailable; set LEAF_SOURCE_SHA')
}

function healthArtifact(sourceSha) {
  return {
    name: 'leaf-platform-health-artifact',
    generateBundle() {
      this.emitFile({
        type: 'asset',
        fileName: 'health.json',
        source: JSON.stringify({
          ok: true,
          service: 'leaf-platform-web',
          component: 'frontend',
          schema_version: 1,
          source_sha: sourceSha,
        }),
      })
    },
  }
}

// Lane C frontend. Dev server on 5175 so it never collides with the
// Lane D backend (8130). API base + mock mode are controlled via env
// (see src/api.js): VITE_MOCK=1 (default) demos with no backend.
const sourceSha = buildHash()

export default defineConfig({
  plugins: [react(), healthArtifact(sourceSha)],
  define: {
    __BUILD_HASH__: JSON.stringify(sourceSha),
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
