import { describe, expect, it } from 'vitest'
import { PRODUCT_SURFACES, surfaceContract } from './productSurfaces.js'
import { familiesForSurface } from '../lib/surfaceRails.js'
import fixture from './familyCapabilities.json'

const families = Object.keys(fixture).map((family_id) => ({ family_id }))

function catalogCapabilities(id) {
  return familiesForSurface(families, id).flatMap(({ family_id }) => fixture[family_id])
}

describe('surface entitlement declarations', () => {
  for (const { id } of PRODUCT_SURFACES) {
    it(`${id} declares its catalog fold and authoring capabilities`, () => {
      const contract = surfaceContract(id)
      const required = new Set(catalogCapabilities(id))
      if (contract.authoring === true) required.add('build')
      expect(contract.entitlements).toEqual([...required].sort())
      expect(Object.isFrozen(contract.entitlements)).toBe(true)
      expect(contract.entitlements).toEqual([...contract.entitlements].sort())
      expect(new Set(contract.entitlements).size).toBe(contract.entitlements.length)
    })
  }

  it('sheets computes an empty declaration from no families and no authoring', () => {
    expect(familiesForSurface(families, 'sheets')).toEqual([])
    expect(surfaceContract('sheets').authoring).toBe(false)
    expect(surfaceContract('sheets').entitlements).toEqual([])
  })

  it('ignoring authoring would omit a required browser capability', () => {
    const withoutAuthoring = [...new Set(catalogCapabilities('browser'))].sort()
    expect(withoutAuthoring).toEqual(['run_read', 'run_write'])
    expect(withoutAuthoring).not.toEqual(surfaceContract('browser').entitlements)
  })
})
