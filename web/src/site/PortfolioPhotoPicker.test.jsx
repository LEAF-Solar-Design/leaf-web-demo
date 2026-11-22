import { scaleTimeout } from '../testTimeScale.js'
import React, { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import PortfolioEditor from './PortfolioEditor.jsx'
import { emptyPortfolio, photoReference } from './portfolioDocument.js'
import { notifyActiveScopeChanged, readActiveScope } from '../auth/activeScope.js'
import { listProjectPhotos, readProjectPhotoPreview } from '../projects/api.js'

const css = readFileSync(resolve(process.cwd(), 'src/site/portfolioEditor.css'), 'utf8')

const auth = vi.hoisted(() => ({ logouts: new Set(), tokens: new Set() }))
vi.mock('../auth.js', () => ({
  subscribeBeforeLogout: callback => { auth.logouts.add(callback); return () => auth.logouts.delete(callback) },
  subscribeTokenStored: callback => { auth.tokens.add(callback); return () => auth.tokens.delete(callback) },
}))
vi.mock('../projects/api.js', () => ({ listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const OTHER = '99999999-9999-4999-8999-999999999999'
const rows = [
  { material_id: '22222222-2222-4222-8222-222222222222', sha256: 'a'.repeat(64), preview_sha256: 'b'.repeat(64), name: 'Cake.jpg' },
  { material_id: '33333333-3333-4333-8333-333333333333', sha256: 'c'.repeat(64), preview_sha256: 'd'.repeat(64), name: 'Bread.jpg' },
]
const deferred = () => {
  let resolve, reject
  const promise = new Promise((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const observers = []
function account(subject) {
  localStorage.setItem('leaf.jwt', `fixture.${btoa(JSON.stringify({ sub: subject, iss: 'fixture' }))}.signature`)
  notifyActiveScopeChanged()
  ;[...auth.tokens].forEach(callback => callback())
}
function Studio({ initial = emptyPortfolio(), projectId = PROJECT, disabled = false, onChange = () => {} }) {
  const [document, setDocument] = useState(initial)
  return <><fieldset disabled={disabled}><PortfolioEditor document={document} projectId={projectId} onChange={next => { onChange(next); setDocument(next) }} /></fieldset>
    <output data-testid="draft">{JSON.stringify(document)}</output></>
}
const draft = () => JSON.parse(screen.getByTestId('draft').textContent)
const gallery = () => screen.getByLabelText('Gallery 1')
const chooser = () => within(gallery()).getByRole('group', { name: 'Choose photos for Selected work' })
const choice = (name = 'Cake.jpg') => within(chooser()).getByRole('button', { name: `Add ${name} to Selected work` })
async function load() {
  const disclosure = gallery().querySelector('details')
  if (!disclosure.open) fireEvent.click(disclosure.querySelector('summary'))
  // Avoid computing accessible names for every existing image's editing buttons.
  fireEvent.click(screen.getByText('Load project photos', { selector: 'button' }))
  await within(gallery()).findByRole('group', { name: 'Choose photos for Selected work' })
}
async function reveal(observer) {
  await act(async () => observer.callback([{ isIntersecting: true }]))
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  auth.logouts.clear(); auth.tokens.clear(); observers.length = 0
  account('account-a')
  listProjectPhotos.mockResolvedValue({ materials: rows })
  readProjectPhotoPreview.mockResolvedValue(new Blob(['preview'], { type: 'image/jpeg' }))
  vi.stubGlobal('IntersectionObserver', class {
    constructor(callback) { this.callback = callback; this.disconnect = vi.fn(); observers.push(this) }
    observe(node) { this.node = node }
  })
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = vi.fn(() => `blob:picker-${URL.createObjectURL.mock.calls.length}`)
    static revokeObjectURL = vi.fn()
  })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

describe('connected portfolio thumbnail chooser', () => {
  it('appears after loading and reads authenticated previews only when a thumbnail approaches the viewport', async () => {
    render(<Studio />)
    expect(screen.queryByRole('group', { name: /Choose photos/ })).not.toBeInTheDocument()
    await load()
    expect(listProjectPhotos).toHaveBeenCalledWith(PROJECT, expect.objectContaining({ expectedScope: readActiveScope(), signal: expect.any(AbortSignal) }))
    expect(within(chooser()).getByText('Cake.jpg')).toBeVisible()
    expect(readProjectPhotoPreview).not.toHaveBeenCalled()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    await reveal(observers[0])
    expect(readProjectPhotoPreview).toHaveBeenCalledTimes(1)
    expect(readProjectPhotoPreview).toHaveBeenCalledWith(PROJECT, rows[0].material_id, expect.objectContaining({ expectedScope: readActiveScope(), signal: expect.any(AbortSignal) }))
    expect(within(choice()).getByRole('img', { name: 'Cake.jpg' })).toHaveAttribute('src', 'blob:picker-1')
    expect(observers[0].disconnect).toHaveBeenCalled()
  })

  it('adds exactly one immutable original reference to the draft, preserves source data and excludes it afterward', async () => {
    const initial = emptyPortfolio(), change = vi.fn()
    initial.categories.push({ slug: 'other', title: 'Other', description: 'Keep me', hero: 0, images: [photoReference(rows[0])] })
    render(<Studio initial={initial} onChange={change} />)
    await load()
    const button = choice()
    expect(button).toHaveAttribute('type', 'button')
    button.focus()
    expect(button).toHaveFocus()
    // A detail-zero click is the native button activation used by keyboard/AT.
    act(() => { button.click(); button.click() })
    fireEvent.doubleClick(button)
    fireEvent.click(button)
    expect(change).toHaveBeenCalledTimes(1)
    expect(draft().categories[0].images).toEqual([photoReference(rows[0])])
    expect(draft().categories[1]).toEqual(initial.categories[1])
    expect(initial.categories[0].images).toEqual([])
    expect(within(chooser()).queryByRole('button', { name: 'Add Cake.jpg to Selected work' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Add project photo to Selected work').querySelector(`option[value="${rows[0].material_id}"]`)).toBeNull()
    fireEvent.change(screen.getByLabelText('Add project photo to Selected work'), { target: { value: rows[1].material_id } })
    expect(draft().categories[0].images).toEqual(rows.map(photoReference))
    expect(within(chooser()).getByText('All project photos are already in this gallery.')).toBeVisible()
  })

  it('serializes rapid activations before the parent rerenders and refuses duplicates and overflow', async () => {
    const initial = emptyPortfolio(), change = vi.fn()
    initial.categories[0].images = [{ ...photoReference(rows[0]), material_id: 'existing-first' }]
    initial.categories.push({ slug: 'other', title: 'Other', description: '', hero: 0,
      images: Array.from({ length: 198 }, (_, index) => ({ ...photoReference(rows[0]), material_id: `existing-${index}` })) })
    const view = render(<Studio initial={initial} onChange={change} />)
    // Keep queries inside the target gallery instead of rescanning 199 photos' labels.
    const target = view.container.querySelector('section[aria-label="Gallery 1"]')
    expect(target).toHaveAccessibleName('Gallery 1')
    fireEvent.click(target.querySelector('details > summary'))
    fireEvent.click(screen.getByText('Load project photos', { selector: 'button' }))
    const picker = await within(target).findByRole('group', { name: 'Choose photos for Selected work' })
    const first = within(picker).getByRole('button', { name: 'Add Cake.jpg to Selected work' })
    const second = within(picker).getByRole('button', { name: 'Add Bread.jpg to Selected work' })
    act(() => { first.click(); first.click(); second.click() })
    expect(change).toHaveBeenCalledTimes(1)
    expect(draft().categories[0].images).toHaveLength(2)
    expect(draft().categories.reduce((sum, gallery) => sum + gallery.images.length, 0)).toBe(200)
    expect(draft().categories[0].images.at(-1)).toEqual(photoReference(rows[0]))
    expect(within(picker).getByRole('button', { name: 'Add Bread.jpg to Selected work' })).toBeDisabled()
    expect(within(target).getByLabelText('Add project photo to Selected work')).toBeDisabled()
    expect(within(target).getByText(/reached its 200 photo limit/)).toBeVisible()
  }, scaleTimeout(15000))

  it('honors a disabled parent fieldset for both selection paths', async () => {
    const change = vi.fn(), view = render(<Studio onChange={change} />)
    await load()
    view.rerender(<Studio onChange={change} disabled />)
    expect(choice()).toBeDisabled()
    fireEvent.click(choice())
    fireEvent.change(screen.getByLabelText('Add project photo to Selected work'), { target: { value: rows[0].material_id } })
    expect(change).not.toHaveBeenCalled()
  })

  it.each(['project', 'account', 'logout', 'unmount'])('fences %s changes, aborts pending previews and revokes created URLs', async transition => {
    const pending = deferred(), change = vi.fn()
    readProjectPhotoPreview.mockResolvedValueOnce(new Blob(['ready'])).mockReturnValueOnce(pending.promise)
    const view = render(<Studio onChange={change} />)
    await load()
    const stale = choice()
    await reveal(observers[0]); await reveal(observers[1])
    const signals = readProjectPhotoPreview.mock.calls.map(call => call[2].signal)
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    act(() => {
      if (transition === 'project') view.rerender(<Studio projectId={OTHER} onChange={change} />)
      if (transition === 'account') account('account-b')
      if (transition === 'logout') { [...auth.logouts].forEach(callback => callback()); localStorage.clear(); notifyActiveScopeChanged() }
      if (transition === 'unmount') view.unmount()
    })
    expect(signals.every(signal => signal.aborted)).toBe(true)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:picker-1')
    fireEvent.click(stale)
    await act(async () => pending.resolve(new Blob(['late'])))
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(change).not.toHaveBeenCalled()
    expect(observers.every(observer => observer.disconnect.mock.calls.length > 0)).toBe(true)
    if (transition !== 'unmount') expect(screen.queryByRole('group', { name: /Choose photos/ })).not.toBeInTheDocument()
    if (transition === 'project' || transition === 'account') {
      await load()
      await reveal(observers.at(-2))
      expect(readProjectPhotoPreview).toHaveBeenLastCalledWith(transition === 'project' ? OTHER : PROJECT, rows[0].material_id, expect.objectContaining({ expectedScope: readActiveScope() }))
      fireEvent.click(choice())
      expect(change).toHaveBeenCalledTimes(1)
    }
  })

  it('refuses selection synchronously on logout before the scope notification', async () => {
    const change = vi.fn()
    render(<Studio onChange={change} />)
    await load()
    const button = choice()
    act(() => { [...auth.logouts].forEach(callback => callback()); button.click() })
    expect(change).not.toHaveBeenCalled()
  })

  it.each(['project', 'account', 'logout'])('discards a photo-list response after %s changes', async transition => {
    const pending = deferred()
    listProjectPhotos.mockReturnValueOnce(pending.promise)
    const view = render(<Studio />)
    fireEvent.click(screen.getByRole('button', { name: 'Load project photos' }))
    const signal = listProjectPhotos.mock.calls[0][1].signal
    act(() => {
      if (transition === 'project') view.rerender(<Studio projectId={OTHER} />)
      if (transition === 'account') account('account-b')
      if (transition === 'logout') { [...auth.logouts].forEach(callback => callback()); localStorage.clear(); notifyActiveScopeChanged() }
    })
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve({ materials: rows }))
    expect(screen.queryByRole('group', { name: /Choose photos/ })).not.toBeInTheDocument()
    if (transition !== 'logout') {
      await load()
      expect(listProjectPhotos).toHaveBeenLastCalledWith(transition === 'project' ? OTHER : PROJECT, expect.objectContaining({ expectedScope: readActiveScope() }))
    } else expect(screen.getByRole('button', { name: 'Load project photos' })).toBeDisabled()
  })

  it('keeps loading, list errors, empty results and preview failures readable', async () => {
    const pending = deferred()
    listProjectPhotos.mockReturnValueOnce(pending.promise)
    render(<Studio />)
    expect(screen.getByText('Use Load project photos after uploading in Photos.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Load project photos' }))
    expect(screen.getByRole('button', { name: 'Loading photos…' })).toBeDisabled()
    await act(async () => pending.reject(new Error('Project photos unavailable.')))
    expect(screen.getByRole('alert')).toHaveTextContent('Project photos unavailable.')
    listProjectPhotos.mockResolvedValueOnce({ materials: [] })
    fireEvent.click(screen.getByRole('button', { name: 'Load project photos' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(screen.queryByRole('group', { name: /Choose photos/ })).not.toBeInTheDocument()
    await load()
    readProjectPhotoPreview.mockRejectedValueOnce(new Error('Preview unavailable.'))
    await reveal(observers[0])
    expect(within(choice()).getByRole('alert')).toHaveTextContent('Photo could not load.')
    expect(within(choice()).getByText('Cake.jpg')).toBeVisible()
    fireEvent.click(choice())
    expect(draft().categories[0].images).toEqual([photoReference(rows[0])])
  })

  it('scopes grid fit, thumbnail sizing, phone fallback, touch targets and focus visibility to the editor', () => {
    expect(css).toMatch(/\.portfolio-editor__photo-picker\s*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)[^}]*min-width: 0;[^}]*max-width: 100%/)
    expect(css).toMatch(/\.portfolio-editor \.portfolio-editor__photo-choice\s*\{[^}]*min-height: 44px;[^}]*min-inline-size: 44px/)
    expect(css).toMatch(/\.portfolio-editor__photo-choice img\s*\{[^}]*width: 100%;[^}]*height: 96px;[^}]*object-fit: contain/)
    expect(css).toMatch(/\.portfolio-editor__photo-choice:focus-visible\s*\{[^}]*outline:/)
    expect(css).toMatch(/@media\(max-width:400px\)\s*\{\s*\.portfolio-editor__photo-picker\s*\{[^}]*grid-template-columns: minmax\(0, 1fr\)/)
  })
})
