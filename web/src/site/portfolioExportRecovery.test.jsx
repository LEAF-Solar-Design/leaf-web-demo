import * as React from 'react'
import * as jsxRuntime from 'react/jsx-runtime'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'

// Execute the actual exporter literal, not a copied implementation. Styling is
// inert here; the independently built artifact supplies browser geometry proof.
const template = readFileSync(resolve(process.cwd(), '../platform/portfolio_page_template.py'), 'utf8')
const source = template.match(/'app\/Gallery\.jsx': b'''([\s\S]*?)'''/)[1]
const design = Object.fromEntries(['photoStyle', 'photoSpanStyle', 'textStyle', 'galleryStyle', 'containerStyle'].map((key) => [key, () => ({})]))
// Compile outside jsdom's typed-array realm, then render using the test's React.
function loadExport(source, page) {
 const module = { exports: {} }
 const compiled = execFileSync(process.execPath, ['-e',
  "const e=require(process.argv[1]);const s=require('node:fs').readFileSync(0,'utf8');process.stdout.write(e.transformSync(s,{loader:'jsx',format:'cjs',jsx:'automatic'}).code)",
  createRequire(resolve(process.cwd(), 'package.json')).resolve('esbuild')], { input: source, encoding: 'utf8', maxBuffer: 1024 * 1024 })
 new Function('require', 'module', 'exports', compiled)(
  (name) => name === 'react' ? React : name === 'react/jsx-runtime' ? jsxRuntime : name === '../portfolio.json' ? page : design, module, module.exports)
 return module.exports.default
}
const Gallery = loadExport(source)
const photo = (id, title) => ({ material_id: id, alt: title, title })
const first = photo('11111111-1111-4111-8111-111111111111', 'Synthetic cake')
const second = photo('22222222-2222-4222-8222-222222222222', 'Synthetic bread')
const category = (image, slug = 'cakes') => ({ slug, images: [image] })
afterEach(() => { cleanup(); vi.restoreAllMocks() })

describe('actual exported empty gallery lifecycle', () => {
  it('renders all-empty and mixed home cards with title, description and safe hero access', () => {
    const homeSource = template.match(/'app\/page\.jsx': b'''([\s\S]*?)'''/)[1]
    const empty = { slug: 'seasonal', title: 'Seasonal work', description: 'Coming later', hero: 0, images: [] }
    const populated = { slug: 'cakes', title: 'Cakes', description: 'Recent work', hero: 0, images: [first] }
    for (const categories of [[empty], [populated, empty]]) {
      const Home = loadExport(homeSource, { headline: 'Selected work', introduction: 'Made with care', categories })
      const view = render(<Home />)
      expect(screen.getByRole('heading', { name: empty.title })).toBeTruthy()
      expect(screen.getByText(empty.description)).toBeTruthy()
      expect(screen.getByText('No photos in this gallery yet.').closest('a').getAttribute('href')).toBe('/seasonal')
      expect(view.container.querySelectorAll('img').length).toBe(categories.length - 1)
      expect(screen.queryByRole('dialog')).toBeNull()
      view.unmount()
    }
  })

  it('renders zero images and transitions populated to empty to single without a stale dialog', () => {
    const empty = { slug: 'cakes', images: [] }
    const view = render(<><nav data-testid="background">Home</nav><Gallery page={{}} category={empty} /></>)
    expect(screen.getByRole('status').textContent).toBe('No photos in this gallery yet.')
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    const show = category => <><nav data-testid="background">Home</nav><Gallery page={{}} category={category} /></>
    view.rerender(show({ slug: 'cakes', images: [first, second] }))
    const opener = screen.getByRole('button', { name: 'View Synthetic cake' })
    fireEvent.click(opener)
    expect(screen.getByTestId('background').inert).toBe(true)
    view.rerender(show(empty))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByTestId('background').inert).not.toBe(true)
    expect(document.activeElement).toBe(screen.getByRole('status'))
    expect(opener.isConnected).toBe(false)
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(screen.queryByRole('dialog')).toBeNull()
    view.rerender(show(category(second)))
    expect(screen.queryByRole('status')).toBeNull()
    const next = screen.getByRole('button', { name: 'View Synthetic bread' })
    fireEvent.click(next)
    expect(screen.getByRole('dialog', { name: second.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(next)
  })
})

describe('exported viewer photo arrow navigation', () => {
  it('keeps image, caption, bounds and opener focus together through repeated arrows', () => {
    const third = photo('33333333-3333-4333-8333-333333333333', 'Synthetic pastry')
    render(<Gallery page={{}} category={{ slug: 'work', images: [first, second, third] }} />)
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    fireEvent.click(trigger)
    const check = image => {
      const dialog = screen.getByRole('dialog', { name: image.alt })
      expect(within(dialog).getByRole('img', { name: image.alt }).getAttribute('src')).toContain(image.material_id)
      expect(within(dialog).getByText(image.title)).toBeTruthy()
      expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Close image' }))
    }
    fireEvent.keyDown(window, { key: 'ArrowLeft' }); check(first)
    fireEvent.keyDown(window, { key: 'ArrowRight' }); check(second)
    for (let i = 0; i < 6; i++) fireEvent.keyDown(window, { key: 'ArrowRight', repeat: true })
    check(third)
    fireEvent.keyDown(window, { key: 'ArrowLeft' }); check(second)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })

  it('skips hidden photos and leaves modified arrows and single-photo bounds alone', () => {
    const hidden = { ...second, display: { hidden: true } }
    const third = photo('33333333-3333-4333-8333-333333333333', 'Synthetic pastry')
    const view = render(<Gallery page={{}} category={{ slug: 'work', images: [first, hidden, third] }} />)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    for (const modifiers of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }, { shiftKey: true }]) {
      fireEvent.keyDown(window, { key: 'ArrowRight', ...modifiers })
      expect(screen.getByRole('dialog', { name: first.alt })).toBeTruthy()
    }
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(screen.getByRole('dialog', { name: third.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    view.rerender(<Gallery page={{}} category={category(first, 'single')} />)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    for (const key of ['ArrowLeft', 'ArrowRight']) {
      fireEvent.keyDown(window, { key })
      expect(screen.getByRole('dialog', { name: first.alt })).toBeTruthy()
    }
  })

  it('retires the old image error state when arrows select another photo', () => {
    render(<Gallery page={{}} category={{ slug: 'work', images: [first, second] }} />)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    const old = within(screen.getByRole('dialog')).getByRole('img', { name: first.alt })
    fireEvent.error(old)
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Retry photo' })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    fireEvent.error(old)
    const dialog = screen.getByRole('dialog', { name: second.alt })
    expect(within(dialog).getByRole('img', { name: second.alt }).getAttribute('src')).toContain(second.material_id)
    expect(within(dialog).queryByRole('button', { name: 'Retry photo' })).toBeNull()
  })
})

describe('exported viewer background exclusion', () => {
  for (const closeBy of ['Close', 'Escape']) it(`restores prior inert values before ${closeBy} returns focus`, () => {
    render(<><nav data-testid="background"><a href="/">Home</a></nav>
      <aside data-testid="already-inert" ref={node => { if (node) node.inert = true }}>Hidden before opening</aside>
      <Gallery page={{}} category={category(first)} /></>)
    const background = screen.getByTestId('background')
    const already = screen.getByTestId('already-inert')
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    fireEvent.click(trigger)
    expect(background.inert).toBe(true)
    expect(already.inert).toBe(true)
    const focus = trigger.focus.bind(trigger)
    vi.spyOn(trigger, 'focus').mockImplementation(() => {
      expect(background.inert).not.toBe(true)
      focus()
    })
    if (closeBy === 'Close') fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close image' }))
    else fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(background.inert).not.toBe(true)
    expect(already.inert).toBe(true)
    expect(document.activeElement).toBe(trigger)
  })

  it('restores background after category replacement and unmount', () => {
    const view = image => <><nav data-testid="background">Home</nav><Gallery page={{}} category={category(image, image.material_id)} /></>
    const mounted = render(view(first))
    const background = screen.getByTestId('background')
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    expect(background.inert).toBe(true)
    mounted.rerender(view(second))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(background.inert).not.toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic bread' }))
    expect(background.inert).toBe(true)
    mounted.unmount()
    expect(background.inert).not.toBe(true)
  })
})

describe('actual exported portfolio image failure recovery', () => {
  for (const retryFrom of ['grid', 'viewer']) {
    it(`shares retry attempts from the ${retryFrom} without changing an adjacent image`, () => {
      const view = render(<Gallery page={{}} category={{ slug: 'work', images: [first, second] }} />)
      const [tile, adjacent] = view.container.querySelectorAll('figure')
      const opener = within(tile).getByRole('button', { name: 'View ' + first.alt })
      fireEvent.click(opener)
      const dialog = within(screen.getByRole('dialog'))
      fireEvent.error(tile.querySelector('img'))
      fireEvent.error(dialog.getByRole('img'))
      if (retryFrom === 'grid') {
        fireEvent.keyDown(window, { key: 'Escape' })
        fireEvent.click(within(tile).getByRole('button', { name: 'Retry photo' }))
        expect(document.activeElement).toBe(opener)
        fireEvent.click(opener)
      } else {
        fireEvent.click(dialog.getByRole('button', { name: 'Retry photo' }))
      }
      const currentDialog = within(screen.getByRole('dialog'))
      for (const image of [tile.querySelector('img'), currentDialog.getByRole('img')]) {
        expect(image.getAttribute('src')).toBe(`/images/${first.material_id}.jpg?retry=1`)
        fireEvent.load(image)
      }
      expect(adjacent.querySelector('img').getAttribute('src')).toBe(`/images/${second.material_id}.jpg`)
      expect(document.activeElement).toBe(currentDialog.getByRole('button', { name: 'Close image' }))
      fireEvent.keyDown(window, { key: 'Escape' })
      expect(document.activeElement).toBe(opener)
      expect(within(tile).queryByRole('button', { name: 'Retry photo' })).toBeNull()
    })
  }

  it('detects an image that failed before hydration attached its error handler', () => {
    vi.spyOn(HTMLImageElement.prototype, 'complete', 'get').mockReturnValue(true)
    vi.spyOn(HTMLImageElement.prototype, 'naturalWidth', 'get').mockReturnValue(0)
    render(<Gallery page={{}} category={category(first)} />)
    expect(screen.getByRole('img', { name: 'Photograph unavailable: Synthetic cake' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry photo' })).toBeTruthy()
  })

  it('shows a stable failure and retries the same material without nested buttons', () => {
    render(<Gallery page={{}} category={category(first)} />)
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    const old = screen.getByRole('img', { name: first.alt })
    fireEvent.error(old)
    expect(screen.getByRole('img', { name: 'Photograph unavailable: Synthetic cake' })).toBeTruthy()
    const retry = screen.getByRole('button', { name: 'Retry photo' })
    expect(retry.closest('button button')).toBeNull()
    fireEvent.click(retry)
    const image = screen.getByRole('img', { name: first.alt })
    expect(image.getAttribute('src')).toBe(`/images/${first.material_id}.jpg?retry=1`)
    expect(document.activeElement).toBe(trigger)
    fireEvent.error(old)
    fireEvent.load(image)
    expect(screen.queryByRole('button', { name: 'Retry photo' })).toBeNull()
    expect(screen.getByText(first.title)).toBeTruthy()
  })

  it('keeps viewer Retry reachable through keyboard navigation and viewport changes', () => {
    render(<Gallery page={{}} category={category(first)} />)
    const trigger = screen.getByRole('button', { name: 'View Synthetic cake' })
    fireEvent.click(trigger)
    const dialog = screen.getByRole('dialog')
    fireEvent.error(within(dialog).getByRole('img', { name: first.alt }))
    const close = within(dialog).getByRole('button', { name: 'Close image' })
    const retry = within(dialog).getByRole('button', { name: 'Retry photo' })
    // jsdom has no scrolling implementation for this browser API.
    retry.scrollIntoView = vi.fn()
    close.focus(); fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(retry)
    expect(retry.scrollIntoView).toHaveBeenCalledWith({ block: 'center', inline: 'nearest' })
    fireEvent.resize(window)
    expect(document.activeElement).toBe(retry)
    expect(retry.scrollIntoView).toHaveBeenCalledTimes(2)
    fireEvent.keyDown(window, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    close.scrollIntoView = vi.fn()
    fireEvent.resize(window)
    expect(close.scrollIntoView).not.toHaveBeenCalled()
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(retry)
    expect(retry.scrollIntoView).toHaveBeenCalledTimes(3)
    fireEvent.click(retry)
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    trigger.scrollIntoView = vi.fn()
    fireEvent.resize(window)
    expect(trigger.scrollIntoView).not.toHaveBeenCalled()
  })

  it('retires the previous category viewer and its failed image state', () => {
    const view = render(<Gallery page={{}} category={category(first)} />)
    fireEvent.click(screen.getByRole('button', { name: 'View Synthetic cake' }))
    const old = within(screen.getByRole('dialog')).getByRole('img', { name: first.alt })
    fireEvent.error(old)
    view.rerender(<Gallery page={{}} category={category(second, 'breads')} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.error(old)
    expect(screen.queryByRole('button', { name: 'Retry photo' })).toBeNull()
    expect(screen.getByRole('img', { name: second.alt }).getAttribute('src')).toContain(second.material_id)
  })
})


describe('current material selection after document edits', () => {
  it('uses the last visible opener position after a reorder and removal', () => {
    const third = photo('33333333-3333-4333-8333-333333333333', 'Third photo')
    const gallery = images => <Gallery page={{}} category={{ slug: 'work', images }} />
    const view = render(gallery([first, second, third]))
    fireEvent.click(screen.getByRole('button', { name: 'View ' + first.alt }))
    view.rerender(gallery([second, first, third]))
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    view.rerender(gallery([second, third]))
    expect(screen.getByRole('dialog', { name: third.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View ' + third.alt }))
  })

  it('focuses the visible photo at the removed opener position after navigation', () => {
    const third = photo('33333333-3333-4333-8333-333333333333', 'Third photo')
    const gallery = images => <Gallery page={{}} category={{ slug: 'work', images }} />
    const view = render(gallery([first, second, third]))
    fireEvent.click(screen.getByRole('button', { name: 'View ' + second.alt }))
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    view.rerender(gallery([first, third]))
    expect(screen.getByRole('dialog', { name: third.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View ' + third.alt }))
  })

  it('focuses the last visible photo when the removed opener was last', () => {
    const gallery = images => <Gallery page={{}} category={{ slug: 'work', images }} />
    const view = render(gallery([first, second]))
    fireEvent.click(screen.getByRole('button', { name: 'View ' + second.alt }))
    view.rerender(gallery([first]))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View ' + first.alt }))
  })

  it('focuses the empty status when the only photo is hidden', () => {
    const view = render(<Gallery page={{}} category={category(first)} />)
    fireEvent.click(screen.getByRole('button', { name: 'View ' + first.alt }))
    view.rerender(<Gallery page={{}} category={category({ ...first, display: { hidden: true } })} />)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByRole('button')).toBeNull()
    const status = screen.getByRole('status')
    expect(status.textContent).toBe('No photos in this gallery yet.')
    expect(document.activeElement).toBe(status)
  })

  for (const closeBy of ['Escape', 'Close', 'Navigate then Close']) {
    it(`returns focus to the updated grid opener on ${closeBy}`, () => {
      const original = { ...first, preview_sha256: 'a'.repeat(64) }
      const replacement = { ...original, alt: 'Updated portrait', preview_sha256: 'b'.repeat(64) }
      const gallery = image => <Gallery page={{}} category={{ slug: 'work', images: [image, second] }} />
      const view = render(gallery(original))
      const opener = screen.getByRole('button', { name: 'View ' + original.alt })
      const previousPreview = within(opener).getByRole('img')
      fireEvent.click(opener)
      view.rerender(gallery(replacement))
      expect(opener.isConnected).toBe(true)
      expect(previousPreview.isConnected).toBe(false)
      fireEvent.error(previousPreview)
      if (closeBy === 'Navigate then Close') {
        fireEvent.keyDown(window, { key: 'ArrowRight' })
        expect(screen.getByRole('dialog', { name: second.alt })).toBeTruthy()
      }
      if (closeBy === 'Escape') fireEvent.keyDown(window, { key: 'Escape' })
      else fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Close image' }))
      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View ' + replacement.alt }))
      expect(within(opener).getByRole('img', { name: replacement.alt })).toBeTruthy()
    })
  }

  it('updates caption and alt for the same material without moving retry focus', () => {
    const view = render(<Gallery page={{}} category={category(first)} />)
    fireEvent.click(screen.getByRole('button', { name: 'View ' + first.alt }))
    const dialog = screen.getByRole('dialog')
    fireEvent.error(within(dialog).getByRole('img'))
    const retry = within(dialog).getByRole('button', { name: 'Retry photo' })
    retry.focus()
    const edited = { ...first, title: 'Fresh multiline caption\nSecond line', alt: 'Fresh portrait description' }
    view.rerender(<Gallery page={{}} category={category(edited)} />)
    expect(screen.getByRole('dialog', { name: edited.alt })).toBe(dialog)
    expect(dialog.querySelector('p').textContent).toBe('The photo could not load. Check your connection and try again.')
    expect(within(dialog).getByText((_, node) => node.tagName === 'P' && node.textContent === edited.title).textContent).toBe(edited.title)
    expect(document.activeElement).toBe(retry)
    expect(within(dialog).getByRole('img', { name: 'Photograph unavailable: ' + edited.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'View ' + edited.alt }))
  })

  it('keeps material identity after reorder and arrows use the new order', () => {
    const third = photo('33333333-3333-4333-8333-333333333333', 'Third synthetic work')
    const view = render(<Gallery page={{}} category={{ slug: 'work', images: [first, second, third] }} />)
    fireEvent.click(screen.getByRole('button', { name: 'View ' + second.alt }))
    const edited = { ...second, title: 'Reordered bread', alt: 'Reordered bread portrait' }
    view.rerender(<Gallery page={{}} category={{ slug: 'work', images: [third, edited, first] }} />)
    expect(screen.getByRole('dialog', { name: edited.alt })).toBeTruthy()
    expect(within(screen.getByRole('dialog')).getByText(edited.title)).toBeTruthy()
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(screen.getByRole('dialog', { name: first.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'ArrowRight' })
    expect(screen.getByRole('dialog', { name: first.alt })).toBeTruthy()
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(screen.getByRole('dialog', { name: edited.alt })).toBeTruthy()
  })

  it('replaces preview failure state by digest and ignores detached old image events', () => {
    const original = { ...first, preview_sha256: 'a'.repeat(64) }
    const view = render(<Gallery page={{}} category={category(original)} />)
    fireEvent.click(screen.getByRole('button', { name: 'View ' + first.alt }))
    const dialog = screen.getByRole('dialog'), old = within(dialog).getByRole('img')
    fireEvent.error(old)
    const retry = within(dialog).getByRole('button', { name: 'Retry photo' })
    retry.focus()
    const replacement = { ...original, preview_sha256: 'b'.repeat(64), title: 'Replacement preview', alt: 'Replacement preview alt' }
    view.rerender(<Gallery page={{}} category={category(replacement)} />)
    const current = within(screen.getByRole('dialog', { name: replacement.alt })).getByRole('img')
    expect(current).not.toBe(old)
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Close image' }))
    expect(within(dialog).queryByRole('button', { name: 'Retry photo' })).toBeNull()
    fireEvent.error(old); fireEvent.load(old)
    expect(within(dialog).getByRole('img', { name: replacement.alt })).toBe(current)
    expect(within(dialog).queryByRole('button', { name: 'Retry photo' })).toBeNull()
    fireEvent.load(current)
    expect(current.getAttribute('aria-busy')).toBe('false')
    view.rerender(<Gallery page={{}} category={{ slug: 'cakes', images: [{ ...replacement, display: { hidden: true } }] }} />)
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
