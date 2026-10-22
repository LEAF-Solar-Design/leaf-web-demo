import { describe, expect, it } from 'vitest'
import { PRODUCT_SURFACES, surfaceContract } from './productSurfaces.js'
import { familiesForSurface, PENDING_UPSTREAM_ENTITLEMENT_EXEMPTIONS } from '../lib/surfaceRails.js'
import fixture from './familyCapabilities.json'

const families = Object.keys(fixture).map((family_id) => ({ family_id }))

function catalogCapabilities(id) {
  return familiesForSurface(families, id).flatMap(({ family_id }) => Object.values(fixture[family_id]))
}

describe('surface entitlement declarations', () => {
  it('removes the pending-upstream exemption when Lab joins the shared build lane', () => {
    expect(PENDING_UPSTREAM_ENTITLEMENT_EXEMPTIONS).toEqual({ lab: 'lab' })
    for (const [id, rail] of Object.entries(PENDING_UPSTREAM_ENTITLEMENT_EXEMPTIONS)) {
      expect(PRODUCT_SURFACES.some((surface) => surface.id === id)).toBe(true)
      const contract = surfaceContract(id)
      expect(contract.chrome.rail).toBe(rail)
      expect(contract.authoring).toBe(true)
      expect(contract.entitlements, 'Remove the exemption once build is declared').not.toContain('build')
      expect(contract.builds.routes, 'Remove the exemption once build routes land').toEqual([])
    }
  })
  it('every tool fixture value is a supported capability', () => {
    for (const tools of Object.values(fixture)) {
      for (const capability of Object.values(tools)) {
        expect(['run_read', 'run_write', 'solve']).toContain(capability)
      }
    }
  })

  for (const { id } of PRODUCT_SURFACES) {
    it(`${id} declares its catalog fold, authoring and integration capabilities`, () => {
      const contract = surfaceContract(id)
      const required = new Set(catalogCapabilities(id))
      const pendingUpstream = Boolean(contract.chrome.rail)
        && PENDING_UPSTREAM_ENTITLEMENT_EXEMPTIONS[id] === contract.chrome.rail
      if (contract.authoring === true && !pendingUpstream) required.add('build')
      if (id === 'ops') {
        required.add('run_read')
        required.add('run_write')
      }
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

  it('the catalog-only fold omits the Ops integration read/write requirements', () => {
    const catalogOnly = [...new Set(catalogCapabilities('ops'))].sort()
    expect(familiesForSurface(families, 'ops')).toEqual([])
    expect(catalogOnly).toEqual([])
    expect(catalogOnly).not.toContain('run_read')
    expect(catalogOnly).not.toContain('run_write')
    expect(surfaceContract('ops').entitlements).toEqual(['build', 'run_read', 'run_write'])
    expect(catalogOnly).not.toEqual(surfaceContract('ops').entitlements)
  })
})
