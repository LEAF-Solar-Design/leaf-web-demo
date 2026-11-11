// @vitest-environment node
import { readFileSync } from 'node:fs'
import { expect, test } from 'vitest'

// Reads the config as text: importing it would clear ports and seed drawings.
const config = readFileSync(new URL('../playwright.local.config.mjs', import.meta.url), 'utf8')
const count = (text) => config.split(text).length - 1

test('the link-flow web server is ready only once /app answers', () => {
  expect(count("url: 'http://127.0.0.1:5275/app',")).toBe(1)
  expect(count("url: 'http://127.0.0.1:5275',")).toBe(0)
})

test('moving readiness raises no bound and changes no server option', () => {
  expect(count('timeout: 180_000,')).toBe(1)
  expect(count('timeout: 60_000,')).toBe(1)
  expect(count('workers: 1,')).toBe(1)
  expect(count('reuseExistingServer: false,')).toBe(1)
})
