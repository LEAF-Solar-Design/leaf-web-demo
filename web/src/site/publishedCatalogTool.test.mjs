import assert from 'node:assert/strict'
import test from 'node:test'

import { resolvePublishedCatalogTool } from './publishedCatalogTool.js'

test('uses the refreshed runnable tool and its server-issued catalog digest', () => {
  const provisional = { name: 'authored-cat', capabilities: ['drawing.write'] }
  const canonical = {
    name: 'authored-cat',
    capabilities: ['drawing.write'],
    catalog_digest: 'sha256:canonical',
  }

  assert.equal(
    resolvePublishedCatalogTool(provisional, [
      { name: 'another-tool', catalog_digest: 'sha256:other' },
      canonical,
    ]),
    canonical,
  )
})

test('fails closed while the published tool is absent from the runnable catalog', () => {
  assert.throws(
    () => resolvePublishedCatalogTool({ name: 'authored-cat' }, []),
    /not available in the runnable catalog yet/,
  )
})

test('rejects a provisional tool that has not received a server catalog digest', () => {
  assert.throws(
    () => resolvePublishedCatalogTool(
      { name: 'authored-cat' },
      [{ name: 'authored-cat', capabilities: ['drawing.write'] }],
    ),
    /not available in the runnable catalog yet/,
  )
})
