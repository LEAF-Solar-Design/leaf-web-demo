import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vitest/config'

// The managed project proof's Node contract suite only: the same file, environment and isolation as the
// web-project-proof-contract gate (which starts Vitest with config false). For `vitest run --config` callers.
export default defineConfig({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  test: {
    environment: 'node',
    include: ['e2e/project/projectProof.test.mjs'],
    exclude: ['node_modules/**'],
  },
})
