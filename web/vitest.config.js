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

export default defineConfig({
  // The automatic JSX runtime, matching how vite builds the app. Without it
  // JSX compiles to React.createElement and every spec dies on "React is not
  // defined" — a failure about the runner, not the component.
  esbuild: { jsx: 'automatic' },
  test: {
    environment: 'jsdom',
    globals: false, // explicit imports; a global `expect` hides where it came from
    include: ['src/**/*.test.{js,jsx}'],
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    setupFiles: ['./vitest.setup.js'],
    restoreMocks: true,
  },
})
