import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio } from './portfolioDocument.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {}, subscribeTokenStored: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => null, subscribeActiveScope: () => () => {} }))
vi.mock('../projects/api.js', () => ({ listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))

const document = { ...emptyPortfolio(), categories: [{ slug: 'work', title: 'Work', description: '', hero: 0,
  images: [{ material_id: '22222222-2222-4222-8222-222222222222', sha256: 'a'.repeat(64),
    preview_sha256: 'b'.repeat(64), title: 'Portrait', alt: 'Synthetic portrait' }] }] }
let showModal, close
const descriptors = new Map(['showModal', 'close'].map(key => [key, Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, key)]))

beforeEach(() => {
  showModal = vi.fn(function () { this.open = true })
  close = vi.fn(function () { this.open = false })
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value: showModal })
  Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value: close })
})
afterEach(() => {
  cleanup()
  for (const [key, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(HTMLDialogElement.prototype, key, descriptor)
    else delete HTMLDialogElement.prototype[key]
  }
})

const openPhoto = () => {
  fireEvent.click(screen.getByRole('button', { name: 'Work', exact: true }))
  const trigger = screen.getByRole('button', { name: 'View Synthetic portrait', exact: true })
  fireEvent.click(trigger)
  return trigger
}

it('opens in the browser modal top layer and returns focus on Close image', () => {
  render(<PortfolioPreview document={document} projectId="owned-project" />)
  const trigger = openPhoto()
  const dialog = screen.getByRole('dialog', { name: 'Synthetic portrait' })
  expect(dialog.tagName).toBe('DIALOG')
  expect(showModal).toHaveBeenCalledTimes(1)
  expect(screen.getByRole('button', { name: 'Close image' })).toHaveFocus()
  fireEvent.click(screen.getByRole('button', { name: 'Close image' }))
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
})

it('native Escape cancellation closes the viewer and restores the photo trigger', () => {
  render(<PortfolioPreview document={document} projectId="owned-project" />)
  const trigger = openPhoto()
  const dialog = screen.getByRole('dialog', { name: 'Synthetic portrait' })
  expect(fireEvent(dialog, new Event('cancel', { bubbles: false, cancelable: true }))).toBe(false)
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(trigger).toHaveFocus()
})

it('releases the browser modal when the project scope changes', () => {
  const { rerender } = render(<PortfolioPreview document={document} projectId="owned-project" />)
  openPhoto()
  rerender(<PortfolioPreview document={document} projectId="other-project" />)
  expect(close).toHaveBeenCalledTimes(1)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
})
