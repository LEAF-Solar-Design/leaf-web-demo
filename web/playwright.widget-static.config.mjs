import { defineConfig } from '@playwright/test'
import widgetConfig from './playwright.widgets.config.mjs'

// Separate selection keeps the six-case native recovery gate unchanged.
export default defineConfig({
  ...widgetConfig, testMatch: 'static-clock.case.mjs',
  outputDir: '../artifacts/widget-static-clock',
})
