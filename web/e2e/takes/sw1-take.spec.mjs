import { expect, test } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { runBeat } from './runBeat.mjs'

const beat = JSON.parse(await readFile(new URL('./beats/SW1.json', import.meta.url), 'utf8'))
const origin = process.env.LEAF_TAKE_ORIGIN
if (origin !== undefined && origin !== beat.origin) throw new Error(`LEAF_TAKE_ORIGIN must be ${beat.origin}`)
test.skip(origin === undefined, `Set LEAF_TAKE_ORIGIN=${beat.origin} to run the standalone take`)

test.use({
  viewport: { width: 1600, height: 1000 },
  video: { mode: 'on', size: { width: 1600, height: 1000 } },
  storageState: { cookies: [], origins: [] },
  baseURL: beat.origin,
})

test('SW1 split-guest-drawing-open standalone take', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const record = await runBeat(page, beat, { sync_id: process.env.LEAF_TAKE_SYNC_ID })
  const properties = page.getByRole('complementary', { name: 'Properties', exact: true })
  await expect(properties).toBeVisible()
  for (const text of beat.expect_text) {
    await expect(properties.getByText(text, { exact: true }).filter({ visible: true }).first()).toBeVisible()
  }
  // Tie the layer count to its Drawing row; a bare "1" could match another field.
  await expect(properties.getByTestId('dock-drawing')).toContainText('Layers1Layers shown1')
  const logPath = testInfo.outputPath('SW1.beat.jsonl')
  await writeFile(logPath, `${JSON.stringify(record)}\n`, 'utf8')
  await testInfo.attach('beat-log', { path: logPath, contentType: 'application/x-ndjson' })
})
