import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PortfolioEditor, { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio, importPortfolio, validatePortfolioDocument } from './portfolioDocument.js'
import { listProjectPhotos, readProjectPhotoPreview } from '../projects/api.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {}, subscribeTokenStored: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => null, subscribeActiveScope: () => () => {} }))
vi.mock('../projects/api.js', () => ({ listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const photos = [
  { material_id: '22222222-2222-4222-8222-222222222222', sha256: 'a'.repeat(64), preview_sha256: 'b'.repeat(64), name: 'Cake' },
  { material_id: '33333333-3333-4333-8333-333333333333', sha256: 'c'.repeat(64), preview_sha256: 'd'.repeat(64), name: 'Bread' },
]
const fixture = () => ({
  ...emptyPortfolio(), name: 'Jessica', headline: 'Selected pastry', introduction: 'Made with care', about: 'Baker',
  contact: { email: 'hello@example.com', location: 'Portland' },
  resume: [{ heading: 'Experience', entries: [{ title: 'Chef', subtitle: 'Kitchen', dates: '2024', description: 'Baking' }] }],
  categories: [{ slug: 'cakes', title: 'Cakes', description: 'Recent work', hero: 1,
    images: photos.map(photo => ({ material_id: photo.material_id, sha256: photo.sha256, preview_sha256: photo.preview_sha256, title: photo.name, alt: `${photo.name} photograph` })) }],
})
const importFixture = () => {
  const value = fixture()
  value.schema = 'leaf.portfolio.import.v1'
  value.categories[0].images = value.categories[0].images.map(image => ({ source_sha256: image.sha256, title: image.title, alt: image.alt }))
  return value
}

beforeEach(() => {
  vi.clearAllMocks()
  listProjectPhotos.mockResolvedValue({ materials: photos })
})
afterEach(cleanup)

describe('persisted portfolio photo layout', () => {
  it('keeps legacy validation bytes unchanged and renders Grid without an initial change', () => {
    const original = fixture(), bytes = JSON.stringify(original), change = vi.fn()
    const validated = validatePortfolioDocument(original)
    expect(JSON.stringify(validated)).toBe(bytes)
    expect(Object.hasOwn(validated, 'galleryLayout')).toBe(false)
    const view = render(<><PortfolioEditor document={original} onChange={change} projectId={PROJECT} /><PortfolioPreview document={original} projectId={PROJECT} /></>)
    expect(screen.getByRole('combobox', { name: 'Photo layout' }).value).toBe('grid')
    expect(view.container.querySelector('[data-gallery-layout]')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cakes', exact: true }))
    expect(view.container.querySelector('.portfolio-grid').getAttribute('data-gallery-layout')).toBe('grid')
    expect(change).not.toHaveBeenCalled()
    expect(readProjectPhotoPreview).not.toHaveBeenCalled()
    expect(JSON.stringify(original)).toBe(bytes)
  })

  it('accepts only the exact optional enum in saved and import documents', () => {
    for (const importing of [false, true]) {
      const base = importing ? importFixture() : fixture()
      expect(validatePortfolioDocument(base, importing)).toEqual(base)
      for (const galleryLayout of ['grid', 'single']) {
        const value = { ...base, galleryLayout }
        expect(validatePortfolioDocument(value, importing)).toEqual(value)
      }
      for (const galleryLayout of [undefined, null, true, false, 0, 1, [], {}, '', 'Grid', 'Single column', 'single\n']) {
        expect(() => validatePortfolioDocument({ ...base, galleryLayout }, importing)).toThrow()
      }
      expect(() => validatePortfolioDocument({ ...base, unknown: 'grid' }, importing)).toThrow()
      for (const key of Object.keys(base)) {
        const missing = { ...base, galleryLayout: 'single' }
        delete missing[key]
        expect(() => validatePortfolioDocument(missing, importing)).toThrow()
      }
      const nested = structuredClone(base)
      nested.categories[0].galleryLayout = 'single'
      expect(() => validatePortfolioDocument(nested, importing)).toThrow()
    }
  })

  it('changes only the optional field, drives the gallery, and removes the field when restored to Grid', () => {
    const original = fixture(), bytes = JSON.stringify(original), change = vi.fn(), select = vi.fn()
    function Studio() {
      const [document, setDocument] = useState(original)
      return <><PortfolioEditor document={document} projectId={PROJECT} onChange={next => { change(next); setDocument(next) }} /><PortfolioPreview document={document} projectId={PROJECT} onSelectField={select} /></>
    }
    const view = render(<Studio />)
    const layout = screen.getByRole('combobox', { name: 'Photo layout' })
    fireEvent.change(layout, { target: { value: 'single' } })
    expect(change).toHaveBeenCalledTimes(1)
    expect(change.mock.lastCall[0]).toEqual({ ...original, galleryLayout: 'single' })
    expect(validatePortfolioDocument(change.mock.lastCall[0])).toEqual({ ...original, galleryLayout: 'single' })
    // Home category cards never receive the gallery layout marker.
    expect(view.container.querySelector('[data-gallery-layout]')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Cakes', exact: true }))
    expect(view.container.querySelector('.portfolio-grid').getAttribute('data-gallery-layout')).toBe('single')
    expect([...view.container.querySelectorAll('.portfolio-grid figure')].map(figure => figure.querySelector('button').getAttribute('aria-label'))).toEqual(['View Cake photograph', 'View Bread photograph'])
    fireEvent.click(screen.getByRole('button', { name: 'Edit Cakes gallery title' }))
    expect(select.mock.lastCall[0]).toMatchObject({ field: 'gallery.title', gallery: 'cakes' })
    fireEvent.click(screen.getByRole('button', { name: 'View Cake photograph' }))
    expect(screen.getByRole('dialog', { name: 'Cake photograph' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close image' }))
    fireEvent.change(layout, { target: { value: 'grid' } })
    expect(change).toHaveBeenCalledTimes(2)
    expect(change.mock.lastCall[0]).toEqual(original)
    expect(Object.hasOwn(change.mock.lastCall[0], 'galleryLayout')).toBe(false)
    expect(view.container.querySelector('.portfolio-grid').getAttribute('data-gallery-layout')).toBe('grid')
    expect(JSON.stringify(original)).toBe(bytes)
  })

  it('inherits the existing disabled inspector fieldset', () => {
    const change = vi.fn()
    render(<fieldset disabled><PortfolioEditor document={{ ...fixture(), galleryLayout: 'single' }} projectId={PROJECT} onChange={change} /></fieldset>)
    const layout = screen.getByRole('combobox', { name: 'Photo layout' })
    expect(layout.value).toBe('single')
    expect(layout.matches(':disabled')).toBe(true)
    expect(change).not.toHaveBeenCalled()
  })

  it('retains chosen layouts through actual import photo resolution without adding a legacy default', async () => {
    for (const galleryLayout of [undefined, 'grid', 'single']) {
      const source = importFixture()
      if (galleryLayout !== undefined) source.galleryLayout = galleryLayout
      const raw = JSON.stringify(source)
      const actual = await importPortfolio({ name: 'portfolio.json', size: raw.length, text: async () => raw }, PROJECT, {})
      const expected = fixture()
      if (galleryLayout !== undefined) expected.galleryLayout = galleryLayout
      expect(actual).toEqual(expected)
      expect(Object.hasOwn(actual, 'galleryLayout')).toBe(galleryLayout !== undefined)
      expect(actual.categories[0].images).toEqual(expected.categories[0].images)
    }
    expect(listProjectPhotos).toHaveBeenCalledTimes(3)
  })
})
