import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const root = path.dirname(fileURLToPath(import.meta.url))

// Isolated entry and output: the production app and its bundle budget are untouched.
// vite.config.js exports no shared plugin/resolve settings; mirror its React dedupe.
export default defineConfig({
  root,
  mode: 'production',
  plugins: [react()],
  resolve: { dedupe: ['react', 'react-dom', 'zod', 'recharts'] },
  publicDir: false,
  preview: { host: '127.0.0.1', port: 5195, strictPort: true },
  build: {
    outDir: path.join(root, 'dist-lab-perf'),
    emptyOutDir: true,
    sourcemap: false,
    rollupOptions: { input: path.join(root, 'e2e/lab-perf/harness.html') },
  },
})
