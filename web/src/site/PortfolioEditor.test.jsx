import { useState } from 'react'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import PortfolioEditor from './PortfolioEditor.jsx'
import { emptyPortfolio } from './portfolioDocument.js'

vi.mock('../auth.js', () => ({ subscribeBeforeLogout: () => () => {} }))
vi.mock('../auth/activeScope.js', () => ({
  readActiveScope: () => null,
  subscribeActiveScope: () => () => {},
}))

function TestEditor({ initialDocument }) {
  const [document, setDocument] = useState(initialDocument)
  return <PortfolioEditor document={document} onChange={setDocument} projectId="project-a" />
}

describe('portfolio description review', () => {
  afterEach(cleanup)

  it.each([
    ['Missing gallery description', 'Category description', { description: '  ' }, {}, 'Roof installation at the public library.'],
    ['Missing caption for photo 1', 'Caption 1', {}, { title: '' }, 'Array inspected in July.'],
    ['Review alt text for photo 1', 'Alt text 1', {}, { alt: 'IMG_01.JPG' }, 'Panels beside the skylight.'],
    ['Review alt text for photo 1', 'Alt text 1', {}, { alt: 'site plan (2).png' }, 'Plan showing the entrance and array.'],
  ])('opens the correct collapsed editor for %s (%#)', (issue, label, galleryEdits, photoEdits, repair) => {
    const initialDocument = emptyPortfolio()
    const photo = { material_id: 'roof-1', title: 'Library roof', alt: 'Solar array on the library roof.' }
    const gallery = { ...initialDocument.categories[0], description: 'Library rooftop installation.', images: [photo] }
    initialDocument.categories = [
      gallery,
      { ...gallery, slug: 'installation', title: 'Installation', ...galleryEdits, images: [{ ...photo, ...photoEdits }] },
    ]
    render(<TestEditor initialDocument={initialDocument} />)

    const unaffected = screen.getByRole('region', { name: 'Gallery 1' })
    const affected = screen.getByRole('region', { name: 'Gallery 2' })
    const editor = affected.querySelector('details')
    const field = within(affected).getByLabelText(label)
    const photoEditor = field.closest('details')
    expect(editor.open).toBe(false)
    expect(photoEditor.open).toBe(false)

    screen.getByText('Review gallery descriptions (1)').closest('details').open = true
    fireEvent.click(screen.getByRole('button', { name: issue }))
    expect(editor.open).toBe(true)
    expect(photoEditor.open).toBe(true)
    expect(document.activeElement).toBe(field)
    expect(unaffected.querySelector('details').open).toBe(false)

    fireEvent.change(field, { target: { value: repair } })
    expect(field.value).toBe(repair)
    expect(screen.queryByText('Review gallery descriptions (1)')).toBeNull()
    expect(screen.queryByRole('button', { name: issue })).toBeNull()
  })
})
