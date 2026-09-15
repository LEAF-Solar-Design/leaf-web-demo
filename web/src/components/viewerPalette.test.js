import { describe, expect, it, vi } from 'vitest'
import * as THREE from 'three'
import { recolorLayerGroups } from './Viewer.jsx'

function paletteFixture() {
  const panels = new THREE.Group()
  panels.add(new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshBasicMaterial({ color: '#9fb3c8' })))
  panels.add(new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: '#9fb3c8' })))
  const defpoints = new THREE.Group()
  defpoints.add(new THREE.Mesh(new THREE.BufferGeometry(), new THREE.MeshStandardMaterial({ color: '#9fb3c8', emissive: '#9fb3c8' })))
  defpoints.add(new THREE.Object3D())
  return new Map([['Panels', panels], ['Defpoints', defpoints]])
}

describe('recolorLayerGroups', () => {
  it('updates direct layer materials and emissive in place, resolving each layer once', () => {
    const groups = paletteFixture()
    const children = [...groups.values()].flatMap((group) => group.children)
    const materials = children.map((child) => child.material)
    const colorForLayer = vi.fn((layer) => (layer === 'Panels' ? '#7fd6a6' : '#123456'))

    expect(recolorLayerGroups(groups, colorForLayer)).toBe(3)
    expect(colorForLayer).toHaveBeenCalledTimes(2)
    expect(colorForLayer).toHaveBeenNthCalledWith(1, 'Panels')
    expect(colorForLayer).toHaveBeenNthCalledWith(2, 'Defpoints')
    expect(materials[0].color.getHexString()).toBe('7fd6a6')
    expect(materials[1].color.getHexString()).toBe('7fd6a6')
    expect(materials[2].color.getHexString()).toBe('123456')
    expect(materials[2].emissive.getHexString()).toBe('123456')
    children.forEach((child, index) => expect(child.material).toBe(materials[index]))
  })

  it('returns zero without changing materials for an invalid map or callback', () => {
    const groups = paletteFixture()
    const colorForLayer = vi.fn(() => '#7fd6a6')
    const children = [...groups.values()].flatMap((group) => group.children)
    const materials = children.map((child) => child.material)

    expect(recolorLayerGroups(null, colorForLayer)).toBe(0)
    expect(recolorLayerGroups(groups, null)).toBe(0)
    expect(colorForLayer).not.toHaveBeenCalled()
    children.forEach((child, index) => {
      expect(child.material).toBe(materials[index])
      if (child.material) expect(child.material.color.getHexString()).toBe('9fb3c8')
      if (child.material?.emissive) expect(child.material.emissive.getHexString()).toBe('9fb3c8')
    })
  })
})
