import { defineConfig } from '@playwright/test'
import base from './playwright.config.mjs'

// Exercise the populated Solar rail enabled on staging, using the same APIs.
export default defineConfig({
  ...base,
  testMatch: 'workspace-docked-chrome.spec.mjs',
  webServer: {
    ...base.webServer,
    env: {
      ...base.webServer.env,
      VITE_CAD_EDIT: '1',
      VITE_SOLAR_SETTINGS_FORM: '1',
      VITE_SOLAR_FLOW_RAIL: '1',
    },
  },
})
