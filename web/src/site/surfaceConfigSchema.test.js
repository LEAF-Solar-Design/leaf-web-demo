// Standardization slice 7b. contract/surface-config.v1.schema.json's own
// description says "productSurfaces.js is the ENUM source of truth" and
// "the leaf-web-demo half pins byte-for-byte equality between the two by
// test" — this is that test, on the leaf-web-demo side (the mushy-code lane
// that authored the schema cannot read this file at all, per its $comment).
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { PRODUCT_SURFACES } from './productSurfaces.js'

// process.cwd(), not import.meta.url: vitest runs from web/ (see
// ProductSurfaceTabs.test.jsx's own readFileSync for the same convention),
// and the schema lives one level up, at the repo root's contract/.
const SCHEMA_PATH = `${process.cwd()}/../contract/surface-config.v1.schema.json`
const schema = JSON.parse(readFileSync(SCHEMA_PATH, 'utf8'))

describe('Surface Contract — schema', () => {
  it('declares exactly the surface ids productSurfaces.js ships', () => {
    const schemaIds = Object.keys(schema.properties).sort()
    const contractIds = PRODUCT_SURFACES.map(({ id }) => id).sort()
    expect(schemaIds).toEqual(contractIds)
  })

  it('every declared overlay slot name is a real top-level key on every contract record', () => {
    const overlaySlots = Object.keys(schema.$defs.surfaceOverlay.properties)
    // Non-empty and stable: a schema that regressed to declaring nothing
    // would make this whole test pass vacuously.
    expect(overlaySlots.length).toBeGreaterThan(0)
    for (const surface of PRODUCT_SURFACES) {
      for (const slot of overlaySlots) {
        expect(
          Object.prototype.hasOwnProperty.call(surface.contract, slot),
          `productSurfaces.js's "${surface.id}" record has no "${slot}" key, `
            + 'but the schema declares it as an overlay-able slot',
        ).toBe(true)
      }
    }
  })

  it('chrome.tab (the slice-5b field the schema names by name) is a real chrome field everywhere', () => {
    expect(schema.$defs.chromeSlot.properties).toHaveProperty('tab')
    for (const surface of PRODUCT_SURFACES) {
      expect(Object.prototype.hasOwnProperty.call(surface.contract.chrome, 'tab')).toBe(true)
    }
  })

  it('fails the whole file closed on an unknown surface id or slot name (additionalProperties:false)', () => {
    expect(schema.additionalProperties).toBe(false)
    expect(schema.$defs.surfaceOverlay.additionalProperties).toBe(false)
  })
})
