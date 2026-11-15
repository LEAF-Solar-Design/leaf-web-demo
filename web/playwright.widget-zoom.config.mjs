import { defineConfig } from '@playwright/test'
import config from './playwright.widgets.config.mjs'
export default defineConfig({...config, testMatch: 'widget-zoom.case.mjs', outputDir: '../artifacts/widget-zoom'})
