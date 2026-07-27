import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: '.',
  testMatch: 'workspace-controller.spec.mjs',
  reporter: 'list',
  workers: 1,
})
