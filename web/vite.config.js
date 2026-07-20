import { execSync } from 'node:child_process'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

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
  plugins: [react()],
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
