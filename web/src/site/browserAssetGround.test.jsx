import { useContext } from 'react'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { BrowserArtifactContext, BrowserAssetGround } from './BrowserAssetGround.jsx'

vi.mock('./ProjectBoardGround.jsx', () => ({ ProjectBoardGround: ({ panel, active }) => <div hidden={!active}>{panel}</div> }))
afterEach(cleanup)

let handoff
function Output() {
  handoff = useContext(BrowserArtifactContext)
  return <label>Campaign input<input defaultValue="Saved request" /></label>
}
const props = { browserActive: true, signedIn: true, accountScope: 'account:one', projectId: 'project-one',
  workspaceProject: { kind: 'project', label: 'Pastry shop' }, panel: <Output /> }

it('starts empty with closed overlays and retains the existing panel input across toggles', () => {
  const { container } = render(<BrowserAssetGround {...props} />)
  expect(screen.getByText('No app to show yet')).toBeTruthy()
  expect(screen.getByText('Pastry shop')).toBeTruthy()
  expect(container.querySelector('iframe')).toBeNull()
  const toggle = screen.getByRole('button', { name: 'Project' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(document.getElementById(toggle.getAttribute('aria-controls')).hasAttribute('inert')).toBe(true)
  fireEvent.click(toggle)
  const field = screen.getByLabelText('Campaign input')
  fireEvent.change(field, { target: { value: 'Keep this request' } })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Close Project' }))
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(toggle.getAttribute('aria-expanded')).toBe('false')
  expect(document.activeElement).toBe(toggle)
  fireEvent.click(toggle)
  expect(screen.getByLabelText('Campaign input')).toBe(field)
  expect(field.value).toBe('Keep this request')
})

it('hands HTML to a full viewport sandbox and retains it across toggles and profiles', () => {
  const { rerender } = render(<BrowserAssetGround {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  act(() => expect(handoff({ html: '<h1>Verified output</h1>', name: 'pastry.html' })).toBe(true))
  const frame = screen.getByTitle('Verified preview: pastry.html')
  expect(frame.getAttribute('sandbox')).toBe('allow-scripts allow-downloads')
  expect(frame.getAttribute('srcdoc')).toBe('<h1>Verified output</h1>')
  expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  expect(screen.getByTitle('Verified preview: pastry.html')).toBe(frame)
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Studio' }))
  rerender(<BrowserAssetGround {...props} browserActive={false} />)
  rerender(<BrowserAssetGround {...props} />)
  expect(screen.getByTitle('Verified preview: pastry.html')).toBe(frame)
})

it('keeps one campaign input and hands off to Browser while another profile is selected', () => {
  const onSelect = vi.fn()
  const { container, rerender } = render(<BrowserAssetGround {...props} profile="cad" browserActive={false} onSelect={onSelect} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  const field = screen.getByLabelText('Campaign input')
  fireEvent.change(field, { target: { value: 'Retain across profiles' } })
  const receiver = handoff
  fireEvent.click(screen.getByRole('button', { name: 'Studio' }))
  rerender(<BrowserAssetGround {...props} profile="ios" browserActive={false} onSelect={onSelect} />)
  act(() => expect(receiver({ html: '<p>Exact verified bytes</p>', name: 'shop.html' })).toBe(true))
  expect(onSelect).toHaveBeenCalledWith('browser')
  rerender(<BrowserAssetGround {...props} profile="browser" onSelect={onSelect} />)
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
  expect(screen.getByTitle('Verified preview: shop.html').getAttribute('srcdoc')).toBe('<p>Exact verified bytes</p>')
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  expect(screen.getByLabelText('Campaign input')).toBe(field)
  expect(field.value).toBe('Retain across profiles')
  expect(screen.getAllByLabelText('Campaign input')).toHaveLength(1)
})

it('does not consume Escape when no environment panel is open', () => {
  render(<BrowserAssetGround {...props} />)
  const event = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
  act(() => window.dispatchEvent(event))
  expect(event.defaultPrevented).toBe(false)
})

it('resets entry on an auth notification even before any preview bytes exist', () => {
  const { container } = render(<BrowserAssetGround {...props} />)
  fireEvent.click(screen.getByRole('button', { name: 'Project' }))
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('true')
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' })))
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
  expect(screen.getByRole('button', { name: 'Project' }).getAttribute('aria-expanded')).toBe('false')
})

it.each([
  { projectId: 'project-two' },
  { accountScope: 'account:two' },
  { signedIn: false },
])('clears bytes and rejects an old handoff on scope change: %j', changed => {
  const { container, rerender } = render(<BrowserAssetGround {...props} />)
  const oldHandoff = handoff
  act(() => handoff({ html: '<p>Private</p>', name: 'private.html' }))
  rerender(<BrowserAssetGround {...props} {...changed} />)
  expect(container.querySelector('iframe')).toBeNull()
  expect(container.querySelector('.workspace-environment').dataset.entered).toBe('false')
  act(() => expect(oldHandoff({ html: '<p>Stale</p>', name: 'stale.html' })).toBe(false))
  expect(container.querySelector('iframe')).toBeNull()
  rerender(<BrowserAssetGround {...props} />)
  act(() => expect(oldHandoff({ html: '<p>Still stale</p>', name: 'stale.html' })).toBe(false))
  expect(container.querySelector('iframe')).toBeNull()
})

it('fences auth changes before updated scope props arrive and rejects an unmounted receiver', () => {
  const { container, unmount } = render(<BrowserAssetGround {...props} />)
  const oldHandoff = handoff
  act(() => handoff({ html: '<p>Private</p>', name: 'private.html' }))
  // A removed token is a different principal even before App updates its props.
  act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'leaf.jwt' })))
  expect(container.querySelector('iframe')).toBeNull()
  act(() => expect(oldHandoff({ html: '<p>Stale</p>', name: 'stale.html' })).toBe(false))
  const receiver = handoff
  unmount()
  expect(receiver({ html: '<p>Late</p>', name: 'late.html' })).toBe(false)
})

it('keeps phone panels inside the viewport with an internal scroller and sticky close controls', () => {
  const css = readFileSync(`${process.cwd()}/src/site/browserAssetGround.css`, 'utf8')
  expect(css).toContain('calc(100vw - 24px)')
  expect(css).toContain('overscroll-behavior: contain')
  expect(css).toContain('position: sticky; top: 0')
  expect(css).toContain('.browser-asset-ground { position: fixed; inset: 0;')
})
