/**
 * Component test runner for the web workspace.
 *
 * This workspace previously had NO unit test infrastructure — only Playwright
 * e2e specs, which cannot exercise a component's failure branches (a rejected
 * decision, an empty proposal, untrusted text) without standing up a whole app
 * and a backend. Those branches are exactly where the T1 operator card's
 * safety lives, so they need a runner that can drive them directly.
 *
 * Deliberately narrow: `include` covers unit specs colocated with source only.
 * The e2e/ directory stays Playwright's — pointing vitest at it would collect
 * specs written for a different runner and fail confusingly.
 */
import { defineConfig } from 'vitest/config'
import { scaleTimeout } from './src/testTimeScale.js'

// Vitest 3 checks these globs against every related input, including the
// wrapper's always-included readers. Only the wrapper has already checked the
// actual diff against these boundaries; direct CLI invocations keep the globs.
const verifiedRelated = process.env.LEAF_VITEST_VERIFIED_RELATED === '1'
  && process.env.LEAF_CI_FULL !== '1' && process.argv.includes('related')

export default defineConfig({
  resolve: { dedupe: ['react', 'react-dom', 'zod', 'recharts'] },
  // The automatic JSX runtime, matching how vite builds the app. Without it
  // JSX compiles to React.createElement and every spec dies on "React is not
  // defined" — a failure about the runner, not the component.
  esbuild: { jsx: 'automatic' },
  test: {
    // LEAF_TEST_TIME_SCALE scales harness ceilings for slower CI workers.
    testTimeout: scaleTimeout(5_000),
    hookTimeout: scaleTimeout(10_000),
    environment: 'jsdom',
    globals: false, // explicit imports; a global `expect` hides where it came from
    include: ['src/**/*.test.{js,jsx}'],
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    setupFiles: ['./vitest.setup.js'],
    // The Python wrapper also enforces these boundaries before graph selection.
    forceRerunTriggers: verifiedRelated ? [] : [
      '**/vitest.config.*', '**/vite.config.*', '**/*config*', '**/*setup*',
      '**/package.json', '**/*lock*', '**/fixtures/**', '**/__fixtures__/**',
      '**/mocks/**', '**/__mocks__/**', '**/public/**', '**/vendor/**',
      '**/generated/**', '**/__generated__/**', '**/*generated*', '**/wasm/**',
      '**/test-utils/**', '**/testing/**', '**/*fixture*', '**/*mock*',
      '**/testTimeScale.*', '**/*testHarness*', '../contract/**', '../scripts/**',
    ],
    restoreMocks: true,
  },
})
