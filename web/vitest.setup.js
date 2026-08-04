/**
 * Test setup for the web workspace.
 *
 * `@testing-library/jest-dom` gives the DOM matchers (toBeInTheDocument,
 * toBeDisabled, ...). Registering them here rather than per-file keeps the
 * specs about behaviour instead of wiring.
 */
import '@testing-library/jest-dom/vitest'
