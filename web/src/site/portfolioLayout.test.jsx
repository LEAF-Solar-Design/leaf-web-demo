import React, { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import PortfolioEditor, { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio, importPortfolio, validatePortfolioDocument } from './portfolioDocument.js'
import { listProjectPhotos, readProjectPhotoPreview } from '../projects/api.js'
import { cloneDocument, resetDesign, selectionIdentity, setOverride } from './portfolioEdits.js'
import { appearanceStyle, containerStyle, navigationOrder, photoStyle, photoSpanStyle, textStyle } from './pageDesign.js'
import ElementInspector, { designElements } from './ElementInspector.jsx'

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

describe('ENV1 contextual editing and projections', () => {
  it.each([
    [['layout'], { gap: -1 }], [['layout'], { maxWidth: 1801 }], [['layout'], { padding: false }], [['layout'], { imageFit: 'cover' }],
    [['images', 0, 'display'], { focal: 'north' }], [['images', 0, 'display'], { span: 3 }], [['images', 0, 'display'], { hidden: false }],
    [['images', 0, 'captionStyle'], { lineHeight: NaN }], [['images', 0, 'captionStyle'], { letterSpacing: -2 }], [['images', 0, 'captionStyle'], { size: 97 }],
  ])('rejects malformed nested design at %j', (path, invalid) => {
    const value = fixture()
    let node = value.categories[0]
    for (const key of path.slice(0, -1)) node = node[key]
    node[path.at(-1)] = invalid
    expect(() => validatePortfolioDocument(value)).toThrow()
  })
  it('evaluates exported web design functions with the same precedence and dimensions as the browser', () => {
    const source = readFileSync(`${process.cwd()}/../platform/page_design.py`, 'utf8')
    const code = source.match(/DESIGN_JS = """([\s\S]*?)"""/)[1].replaceAll('export ', '')
    const portable = runInNewContext(code + ';({textStyle,containerStyle,photoStyle,photoSpanStyle,appearanceStyle,navigationOrder,galleryStyle})')
    const text = { font: 'mono', size: 36, weight: 'bold', color: '#AbCdEf', align: 'right', lineHeight: 2.2, letterSpacing: 4, italic: true, underline: true, hidden: true }
    const box = { align: 'center', columns: 4, mobileColumns: 2, gap: 0, padding: 10, maxWidth: 700, background: '#123456', radius: 12, borderWidth: 2, borderColor: '#ABCDEF', shadow: 'strong' }
    const root = { font: 'classic', scale: 'large', headingWeight: 'bold', spacing: 'airy', width: 'wide', columns: 3, mobileColumns: 2, gap: 7, padding: 9, maxWidth: 900, palette: { background: '#123456', text: '#234567', muted: '#345678', accent: '#456789' }, imageFit: 'cover', aspect: 'square', focal: 'top-left' }
    const photo = { fit: 'contain', aspect: 'portrait', focal: 'bottom-right', radius: 4, span: 2 }
    for (const [name, args, browser] of [
      ['textStyle', [text], textStyle], ['containerStyle', [box], containerStyle],
      ['photoStyle', [photo, root], photoStyle], ['photoStyle', [{}, root], photoStyle],
      ['appearanceStyle', [root], appearanceStyle], ['navigationOrder', [{ order: ['resume'] }], navigationOrder],
      ['photoSpanStyle', [photo, { appearance: root }, { layout: box }], photoSpanStyle],
    ]) expect(JSON.parse(JSON.stringify(portable[name](...args)))).toEqual(JSON.parse(JSON.stringify(browser(...args))))
    expect(portable.galleryStyle({ galleryLayout: 'single' }, {} )['--design-columns']).toBe(1)
    expect(portable.galleryStyle({ galleryLayout: 'single', appearance: root }, { layout: box })).toMatchObject({ '--design-columns': 4, '--design-mobile-columns': 2, gap: 0 })
    expect(photoStyle({}, root)).toMatchObject({ objectFit: 'cover', aspectRatio: '1', objectPosition: 'top left' })
    expect(photoSpanStyle(photo, { galleryLayout: 'single' }, {})).toEqual({ gridColumn: 'span 1', '--design-mobile-span': 1 })
  })

  it('reveals a hidden gallery from outline, edits its caption in place and leaves presentation flags intact', () => {
    const original = fixture()
    original.categories[0].hidden = true
    original.categories[0].layout = { hidden: true }
    original.categories[0].images[0].display = { hidden: true }
    function Studio() {
      const [value, change] = useState(original), [selected, select] = useState(null)
      return <><ElementInspector document={value} selected={selected} onSelect={select} onChange={change} />
        <PortfolioPreview document={value} projectId={PROJECT} selectedElement={selected} onSelectElement={select} onInlineChange={change} />
        <output data-testid="draft">{JSON.stringify(value)}</output></>
    }
    const { container } = render(<Studio />)
    const image = original.categories[0].images[0]
    fireEvent.change(screen.getByLabelText('Elements / Outline'), { target: { value: `${selectionIdentity(image)}:captionStyle` } })
    const caption = container.querySelector('.portfolio textarea[aria-label="Photo caption"]')
    expect(caption).toBeTruthy()
    expect(caption.closest('section').style.display).not.toBe('none')
    fireEvent.focus(caption)
    fireEvent.change(caption, { target: { value: 'Inline caption' } })
    expect(JSON.parse(screen.getByTestId('draft').textContent).categories[0].images[0].title).toBe('Inline caption')
    fireEvent.keyDown(caption, { key: 'Escape' })
    const actual = JSON.parse(screen.getByTestId('draft').textContent)
    expect(actual.categories[0].images[0].title).toBe('Cake')
    expect(actual.categories[0].hidden).toBe(true)
    expect(actual.categories[0].images[0].display.hidden).toBe(true)
    expect(container.querySelector('button textarea, a textarea, button button')).toBeNull()
  })

  it('keeps the selected resume entry after reorder and refuses to retarget a deleted identity', () => {
    const value = fixture(), entry = value.resume[0].entries[0]
    value.resume[0].entries.push({ title: 'Other', subtitle: '', dates: '', description: '' })
    const selected = `${selectionIdentity(entry)}:textStyles.title`, change = vi.fn()
    const { rerender } = render(<ElementInspector document={value} selected={selected} onSelect={() => {}} onChange={change} />)
    const reordered = cloneDocument(value)
    reordered.resume[0].entries.reverse()
    rerender(<ElementInspector document={reordered} selected={selected} onSelect={() => {}} onChange={change} />)
    fireEvent.change(screen.getByLabelText('Text size'), { target: { value: '42' } })
    expect(change.mock.lastCall[0].resume[0].entries[1].textStyles.title.size).toBe(42)
    expect(change.mock.lastCall[0].resume[0].entries[0].textStyles).toBeUndefined()
    reordered.resume[0].entries.pop()
    rerender(<ElementInspector document={reordered} selected={selected} onSelect={() => {}} onChange={change} />)
    expect(screen.queryByLabelText('Text size')).toBeNull()
    expect(screen.getByText('Selected element was removed. Choose an element.')).toBeTruthy()
  })

  it('provides all node families in outline and keeps individual text alignment on a block target', () => {
    const value = fixture()
    const rows = designElements(value)
    expect(rows.map(row => row.label)).toEqual(expect.arrayContaining(['Page appearance', 'Navigation', 'About Heading', 'Contact Heading', 'Resume Heading',
      'Gallery: Cakes', 'Photo: Cake photograph', 'Alt text: Cake photograph', 'Resume section: Experience', 'Resume entry: Chef']))
    value.textStyles = { headline: { align: 'right', size: 40, color: '#123456' } }
    const { container } = render(<PortfolioPreview document={value} projectId={PROJECT} />)
    const headline = within(container).getByText('Selected pastry')
    expect(headline.style.display).toBe('block')
    expect(headline.style.textAlign).toBe('right')
    expect(headline.style.fontSize).toBe('40px')
  })
})


describe('optional ENV1 design data', () => {
  it('retains every optional node family, accepts all-hidden presentation and resets without changing content', () => {
    const legacy = fixture(), value = fixture()
    value.appearance = { font: 'mono', columns: 4, mobileColumns: 2, palette: { background: '#AbCdEF' }, maxWidth: 720 }
    value.textStyles = { headline: { size: 96, weight: 'bold', lineHeight: 2.5, letterSpacing: -1, italic: true, underline: true } }
    value.labels = { aboutHeading: 'About me', contactHeading: 'Say hello', resumeHeading: 'Experience', viewerClose: 'Back' }
    value.nav = { order: ['resume', 'home'], hideHome: true, hidden: true, appearance: { color: '#ABCDEF' }, layout: 'column' }
    value.categories[0].hidden = true
    value.categories[0].layout = { columns: 1, gap: 0, radius: 64, borderWidth: 8, borderColor: '#000000', shadow: 'strong', hidden: true }
    value.categories[0].navLabel = 'Bakes'
    value.categories[0].textStyles = { navigation: { color: '#abcdef' } }
    value.categories[0].images[0].display = { fit: 'cover', aspect: 'portrait', focal: 'bottom-right', span: 2, hidden: true }
    value.categories[0].images[0].captionStyle = { align: 'right' }
    value.resume[0].headingStyle = { font: 'serif' }
    value.resume[0].layout = { hidden: true }
    value.resume[0].entries[0].layout = { padding: 96 }
    value.resume[0].entries[0].textStyles = { dates: { size: 12 } }
    expect(validatePortfolioDocument(value)).toEqual(value)
    expect(resetDesign(value)).toEqual(legacy)
    expect(value.categories[0].hidden).toBe(true)
    expect(photoStyle(value.categories[0].images[0].display)).toMatchObject({ objectFit: 'cover', objectPosition: 'bottom right', aspectRatio: '3 / 4', display: 'none' })
    expect(textStyle(value.textStyles.headline)).toMatchObject({ fontSize: 96, fontWeight: 700, lineHeight: 2.5, letterSpacing: -1, fontStyle: 'italic' })
  })

  it.each([
    ['appearance', {}], ['appearance', { columns: true }], ['appearance', { columns: 1.5 }],
    ['appearance', { columns: 5 }], ['appearance', { mobileColumns: 3 }], ['appearance', { palette: {} }],
    ['appearance', { palette: { text: '#fff' } }], ['appearance', { arbitraryCSS: 'display:none' }],
    ['textStyles', { headline: {} }], ['textStyles', { headline: { size: null } }],
    ['textStyles', { headline: { size: false } }], ['textStyles', { headline: { italic: false } }],
    ['textStyles', { headline: { lineHeight: Infinity } }], ['textStyles', { missing: { size: 12 } }],
    ['nav', { order: ['home', 'home'] }], ['nav', { order: ['external'] }], ['nav', { appearance: { hidden: true } }],
    ['labels', { viewerClose: 'https://example.com' }], ['labels', { aboutHeading: null }],
  ])('rejects malformed %s configuration %j', (field, value) => {
    expect(() => validatePortfolioDocument({ ...fixture(), [field]: value })).toThrow()
  })

  it('keeps semantic identity through immutable reorder and prunes reset ancestors', () => {
    const value = fixture(), entry = value.resume[0].entries[0], identity = selectionIdentity(entry)
    const configured = setOverride(value, identity, ['textStyles', 'title', 'size'], 36)
    expect(entry.textStyles).toBeUndefined()
    const reordered = cloneDocument(configured)
    reordered.resume[0].entries.unshift({ title: 'Other', subtitle: '', dates: '', description: '' })
    expect(selectionIdentity(reordered.resume[0].entries[1])).toBe(identity)
    const reset = setOverride(reordered, identity, ['textStyles', 'title', 'size'], undefined)
    expect(reset.resume[0].entries[1].textStyles).toBeUndefined()
    expect(reset.resume[0].entries[0].title).toBe('Other')
    expect(setOverride(reset, 'stale-identity', ['title'], 'Wrong')).toBe(reset)
  })

  it('keeps imported display configuration while binding only authorized originals', async () => {
    const value = importFixture()
    value.categories[0].images[0].display = { fit: 'cover', focal: 'top-left' }
    value.categories[0].images[0].captionStyle = { color: '#AbCdEf' }
    const actual = await importPortfolio({ name: 'portfolio.json', size: 100, text: async () => JSON.stringify(value) }, PROJECT)
    expect(actual.categories[0].images[0]).toMatchObject({ material_id: photos[0].material_id, sha256: photos[0].sha256,
      preview_sha256: photos[0].preview_sha256, display: { fit: 'cover', focal: 'top-left' }, captionStyle: { color: '#AbCdEf' } })
    expect(actual.categories[0].images[0].source_sha256).toBeUndefined()
  })
})

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
