// @vitest-environment node
import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'

it('puts the inline investor fragment capture first in the document head', () => {
  const source = readFileSync(new URL('./layout.jsx', import.meta.url), 'utf8')
  expect(source).toMatch(/<head>\s*<script dangerouslySetInnerHTML=\{\{ __html: INVESTOR_LINK_CAPTURE_SOURCE \}\} \/>\s*<link rel="preload"/)
})
