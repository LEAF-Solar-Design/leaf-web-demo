import { defineConfig } from '@playwright/test'
import { build, preview } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('.', import.meta.url))
const fixture = '/e2e/widgets/harness.html'
const vite = {
  configFile: false, envFile: false, root, publicDir: false,
  plugins: [react()], resolve: { dedupe: ['react', 'react-dom'] },
  build: { outDir: '../artifacts/widget-regression-bundle', emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL(`.${fixture}`, import.meta.url)) } },
  preview: { host: '127.0.0.1', port: 5197, strictPort: true },
}
if (process.argv.includes('--serve')) {
  await build(vite)
  const server = await preview(vite)
  for (const signal of ['SIGTERM', 'SIGINT']) process.once(signal, () => {
    server.httpServer.close(() => process.exit(0))
  })
}

export default defineConfig({
  testDir: './e2e/widgets', testMatch: '*.spec.mjs', workers: 1, retries: 0,
  timeout: 60000, outputDir: '../artifacts/widget-regression',
  use: { baseURL: 'http://127.0.0.1:5197', browserName: 'chromium',
    viewport: { width: 1600, height: 1000 }, screenshot: 'only-on-failure',
    trace: 'retain-on-failure', video: 'off' },
  webServer: { command: 'node playwright.widgets.config.mjs --serve', cwd: root,
    url: `http://127.0.0.1:5197${fixture}`, reuseExistingServer: false, timeout: 180000 },
})
