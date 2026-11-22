import { defineConfig } from '@playwright/test'
import widgetConfig from './playwright.widgets.config.mjs'

export default defineConfig({
  ...widgetConfig, testMatch: 'widget-unicode.case.mjs',
  outputDir: '../artifacts/widget-unicode',
})
