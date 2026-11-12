import React from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PortfolioPreview } from './PortfolioEditor.jsx'
import { emptyPortfolio } from './portfolioDocument.js'
import { selectionIdentity } from './portfolioEdits.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {}, subscribeTokenStored: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({ readActiveScope: () => null, subscribeActiveScope: () => () => {} }))
vi.mock('../projects/api.js', () => ({ listProjectPhotos: vi.fn(), readProjectPhotoPreview: vi.fn() }))

const PROJECT = '11111111-1111-4111-8111-111111111111'
const fixture = () => ({
  ...emptyPortfolio(),
  categories: [{ slug: 'cakes', title: 'Cakes', description: 'Recent work', hero: 0,
    images: [{ material_id: '22222222-2222-4222-8222-222222222222',
      sha256: 'a'.repeat(64), preview_sha256: 'b'.repeat(64), title: 'Bread.jpg', alt: 'Bread.jpg' }] }],
})

afterEach(cleanup)

describe('PortfolioPreview selection reveal stability', () => {
  it('keeps the Cakes gallery after an unrelated rerender with fresh edit callbacks', () => {
    const document = fixture()
    const props = { document, projectId: PROJECT,
      selectedElement: `${selectionIdentity(document)}:textStyles.headline`,
      onSelectField: vi.fn(), onInlineChange: vi.fn() }
    const { rerender } = render(<PortfolioPreview {...props} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cakes', exact: true }))
    const caption = screen.getByRole('button', { name: 'Edit Cakes caption for Bread.jpg', exact: true })
    expect(caption).toBeInTheDocument()

    rerender(<PortfolioPreview {...props} onSelectField={vi.fn()} onInlineChange={vi.fn()} />)

    expect(screen.getByRole('button', { name: 'Cakes', exact: true })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Edit Cakes caption for Bread.jpg', exact: true })).toBe(caption)
    expect(caption).toBeInTheDocument()
  })

  it('keeps an open photo after an unrelated rerender with a fresh edit callback', () => {
    const document = fixture()
    // Inline editing turns photo clicks into selection; field editing permits the viewer.
    const props = { document, projectId: PROJECT,
      selectedElement: `${selectionIdentity(document.categories[0].images[0])}:captionStyle`,
      onSelectField: vi.fn() }
    const { rerender } = render(<PortfolioPreview {...props} />)

    fireEvent.click(screen.getByRole('button', { name: 'Cakes', exact: true }))
    fireEvent.click(screen.getByRole('button', { name: 'View Bread.jpg', exact: true }))
    const dialog = screen.getByRole('dialog', { name: 'Bread.jpg', exact: true })
    expect(dialog).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Close image', exact: true })).toBeInTheDocument()

    rerender(<PortfolioPreview {...props} onSelectField={vi.fn()} />)

    expect(screen.getByRole('dialog', { name: 'Bread.jpg', exact: true })).toBe(dialog)
    expect(within(dialog).getByRole('button', { name: 'Close image', exact: true })).toBeInTheDocument()
  })

  it('still navigates when the selected element changes to a different view', () => {
    const document = fixture()
    const props = { document, projectId: PROJECT,
      selectedElement: `${selectionIdentity(document.categories[0].images[0])}:captionStyle`,
      onSelectField: vi.fn(), onInlineChange: vi.fn() }
    const { rerender } = render(<PortfolioPreview {...props} />)
    expect(screen.getByRole('button', { name: 'Edit Cakes caption for Bread.jpg', exact: true })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit headline', exact: true })).not.toBeInTheDocument()

    rerender(<PortfolioPreview {...props} selectedElement={`${selectionIdentity(document)}:textStyles.headline`} />)

    expect(screen.getByRole('button', { name: 'Edit headline', exact: true })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit Cakes caption for Bread.jpg', exact: true })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cakes', exact: true })).not.toHaveAttribute('aria-current')
  })
})
