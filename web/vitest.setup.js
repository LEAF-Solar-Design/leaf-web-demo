/**
 * Test setup for the web workspace.
 *
 * `@testing-library/jest-dom` gives the DOM matchers (toBeInTheDocument,
 * toBeDisabled, ...). Registering them here rather than per-file keeps the
 * specs about behaviour instead of wiring.
 */
import '@testing-library/jest-dom/vitest'
import { configure } from '@testing-library/react'
import { scaleTimeout } from './src/testTimeScale.js'

configure({ asyncUtilTimeout: scaleTimeout(1_000) })
