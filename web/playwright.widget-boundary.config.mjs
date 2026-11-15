import { defineConfig } from '@playwright/test'
import widgetConfig from './playwright.widgets.config.mjs'

export default defineConfig({
  ...widgetConfig, testMatch: 'widget-text-boundary.case.mjs',
  outputDir: '../artifacts/widget-text-boundary',
})
