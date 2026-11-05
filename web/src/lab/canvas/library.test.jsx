import { readFileSync } from 'node:fs'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Palette, { BLOCK_MIME } from './Palette.jsx'
import LibraryDetails from './LibraryDetails.jsx'
import LabCanvas from './LabCanvas.jsx'
import { manifests, registry } from './registry.js'
import { intentManifest, readManifest, transformManifests } from '../host/hostProvider.js'
import { sentence } from '../honesty.js'
import { colorLiteralsIn, replacedWordsIn } from '../standardsChecks.js'
import { CATEGORIES, categoryOf, entryMeta, pushRecent, readRecent, RECENT_KEY } from './libraryCategories.js'

// Use a non-identity projection to catch screen coordinates being stored directly.
vi.mock('@xyflow/react', () => ({
  ReactFlowProvider: ({ children }) => children,
  useReactFlow: () => ({ screenToFlowPosition: ({ x, y }) => ({ x: x - 10, y: y - 20 }), getNodes: () => [], fitView() {} }),
  ReactFlow: ({ nodes, onPaneClick, children }) => <div data-testid="pane" onClick={event => {
    if (event.target === event.currentTarget) onPaneClick(event)
  }}>
    {nodes.map(node => <div key={node.id} data-testid="placed-block" data-id={node.id} data-position={JSON.stringify(node.position)} />)}
    {children}
  </div>,
  Background: () => null, Controls: () => null, Handle: () => null,
  Position: { Top: 'top', Bottom: 'bottom' }, BaseEdge: () => null,
  EdgeLabelRenderer: ({ children }) => children, getBezierPath: () => ['', 0, 0],
}))
vi.mock('./dependencyLayout.js', () => ({
  createDependencyLayoutRunner: () => ({ run: async () => ({}), cancel() {} }),
}))

const base = manifests.find(manifest => manifest.id === 'adjust-none')
const transforms = transformManifests()
const count = transforms['count@1.0.0']
const host = readManifest('host-source', 'Dashboard source', { sourceNames: ['jobs'] })
const jobs = readManifest('studio-jobs', 'Jobs')
const capabilities = readManifest('studio-capabilities', 'Capabilities')
const entriesOf = (...items) => Object.fromEntries(items.map(manifest => [`${manifest.id}@${manifest.version.semver}`, manifest]))
const entries = { ...registry, ...transforms, ...entriesOf(host, jobs, capabilities) }
const config = { ...base, id: 'kashbot2-config', title: 'Kashbot configuration', kind: 'settings',
  version: { semver: '1.0.0', code_sha256: 'a'.repeat(64) } }
const group = title => screen.getByRole('region', { name: title })
const entry = (root, id) => root.querySelector(`[data-library-entry="${id}"]`)
const addButtons = root => [...root.querySelectorAll('button[draggable="true"]')]
const search = value => fireEvent.change(screen.getByRole('searchbox'), { target: { value } })
const tap = (button, pointerType = 'touch') => {
  // jsdom may not provide PointerEvent; keep the real pointerdown dispatch.
  const event = new Event('pointerdown', { bubbles: true })
  Object.defineProperty(event, 'pointerType', { value: pointerType })
  fireEvent(button, event)
  fireEvent.click(button)
}
beforeEach(() => { localStorage.clear() })
afterEach(async () => {
  cleanup()
  await act(async () => {})
  vi.unstubAllGlobals()
})

describe('library categories and metadata', () => {
  it('keeps the frozen category order and applies first-match precedence', () => {
    expect(Object.isFrozen(CATEGORIES)).toBe(true)
    expect(CATEGORIES.map(category => [category.id, category.title])).toEqual([
      ['recent', 'Recent'], ['kashbot-v2', 'Kashbot v2'], ['math', 'Math and data'],
      ['sources', 'Sources'], ['tools', 'Tools'], ['kashbot-policy', 'Old Kashbot policy'], ['other', 'Other'],
    ])
    expect(categoryOf(config, { isTool: true })).toBe('tools')
    expect(categoryOf(config)).toBe('kashbot-v2')
    expect(categoryOf({ ...count, id: 'kb2-example' })).toBe('kashbot-v2')
    Object.values(transforms).forEach(manifest => expect(categoryOf(manifest)).toBe('math'))
    ;[host, jobs, capabilities].forEach(manifest => expect(categoryOf(manifest)).toBe('sources'))
    manifests.forEach(manifest => expect(categoryOf(manifest)).toBe('kashbot-policy'))
    expect(categoryOf(intentManifest())).toBe('other')
    expect(categoryOf({ ...base, id: 'third-party' })).toBe('other')
  })

  it('renders categories in order, including the empty Kashbot v2 category, and retains both old groupings', () => {
    const { container } = render(<Palette entries={{ ...entries, ...entriesOf(intentManifest()) }} onAdd={vi.fn()} />)
    expect(screen.getByLabelText('Group blocks')).toHaveValue('category')
    expect([...container.querySelectorAll('h3')].map(heading => heading.textContent)).toEqual([
      'Recent', 'Kashbot v2', 'Math and data', 'Sources', 'Old Kashbot policy', 'Other',
    ])
    expect(within(group('Kashbot v2')).getByText('No Kashbot v2 blocks yet.')).toBeVisible()
    expect(addButtons(group('Math and data'))).toHaveLength(11)
    expect(addButtons(group('Sources'))).toHaveLength(3)
    expect(addButtons(group('Old Kashbot policy'))).toHaveLength(14)
    expect(addButtons(group('Other'))).toHaveLength(1)
    expect(screen.queryByLabelText('Tools')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Group blocks'), { target: { value: 'family' } })
    expect(container.querySelectorAll('[data-element-family]')).toHaveLength(27)
    expect(group('Signals')).toBeVisible()
    expect(group('Rules')).toBeVisible()
    fireEvent.change(screen.getByLabelText('Group blocks'), { target: { value: 'lane' } })
    expect(group('Processing')).toBeVisible()
    expect(group('Strategies')).toBeVisible()
  })

  it('places Tools in category order only with a loader, retaining loading and disabled tools', async () => {
    const toolManifest = { ...base, id: 'tenant-tool', title: 'Tool block', effect: 'write',
      version: { semver: '2.0.0', code_sha256: 'b'.repeat(64) } }
    const tools = [{ id: toolManifest.id, title: 'Run tool', available: true, manifest: toolManifest },
      { id: 'locked', title: 'Locked tool', available: false, reason: 'Not available' }]
    const onAdd = vi.fn()
    const { container } = render(<Palette entries={{ ...entries, ...entriesOf(toolManifest) }} tools={tools} onAdd={onAdd}
      onLoadTools={async () => ({ state: 'on' })} />)
    expect([...container.querySelectorAll('h3,summary')].map(heading => heading.textContent)).toEqual([
      'Recent', 'Kashbot v2', 'Math and data', 'Sources', 'Tools', 'Old Kashbot policy',
    ])
    const toolsGroup = screen.getByLabelText('Tools')
    fireEvent(toolsGroup, new Event('toggle')) // A closed group must not load.
    toolsGroup.open = true
    fireEvent(toolsGroup, new Event('toggle'))
    const button = await within(toolsGroup).findByRole('button', { name: 'Run tool' })
    expect(within(entry(toolsGroup, toolManifest.id)).getByText('Write')).toHaveClass('lab-effect-chip')
    expect(within(entry(toolsGroup, toolManifest.id)).getByText('Runs on the Lab server')).toHaveClass('lab-effect-chip')
    expect(within(toolsGroup).getByRole('button', { name: /Locked tool/ })).toBeDisabled()
    fireEvent.click(button)
    expect(onAdd).toHaveBeenCalledWith(toolManifest)
    expect(entry(group('Old Kashbot policy'), toolManifest.id)).toBeNull()
    for (const grouping of ['lane', 'family', 'category']) {
      fireEvent.change(screen.getByLabelText('Group blocks'), { target: { value: grouping } })
      expect(screen.getByLabelText('Tools').open).toBe(true)
      expect(within(screen.getByLabelText('Tools')).getByRole('button', { name: 'Run tool' })).toBeVisible()
    }
  })

  it('shows honest chips for every placeholder, browser transform, host source and local configuration', () => {
    const { container } = render(<Palette entries={{ ...entries, ...entriesOf(config) }} onAdd={vi.fn()} />)
    for (const manifest of manifests) {
      const row = within(entry(container, manifest.id))
      expect(row.getByText(`${manifest.id}@1.0.0`)).toBeVisible()
      for (const chip of ['Pure', 'Runs on the bot', 'Placeholder']) expect(row.getByText(chip)).toHaveClass('lab-effect-chip')
    }
    for (const manifest of Object.values(transforms)) {
      const row = within(entry(container, manifest.id))
      expect(row.getByText('Runs in your browser')).toHaveClass('lab-effect-chip')
      expect(row.queryByText('Placeholder')).not.toBeInTheDocument()
    }
    expect(within(entry(group('Kashbot v2'), config.id)).getByText('Runs here')).toHaveClass('lab-effect-chip')
    expect(within(entry(container, host.id)).getByText('Read')).toHaveClass('lab-effect-chip')
    expect(within(entry(container, host.id)).getByText('Reads from the host')).toHaveClass('lab-effect-chip')
    expect(entryMeta({ ...config, effect: 'trade' }).effect).toBe('Trade')
  })

  it.each([
    [config, 'Runs here', false], [count, 'Runs in your browser', false],
    [{ ...config, id: 'registered-tool' }, 'Runs on the Lab server', false],
    [base, 'Runs on the bot', true], [host, 'Reads from the host', false],
  ])('derives the placement and placeholder metadata for $id', (manifest, where, placeholder) => {
    expect(entryMeta(manifest)).toEqual({ idVersion: `${manifest.id}@1.0.0`,
      effect: manifest.effect === 'read' ? 'Read' : 'Pure', where, placeholder })
  })

  it('disambiguates all five Strategy titles and searches ids, category, description and Lane without case sensitivity', () => {
    const described = { ...config, description: 'A unique description' }
    const { container } = render(<Palette entries={{ ...entries, ...entriesOf(described) }} onAdd={vi.fn()} />)
    const labels = addButtons(container).map(button => button.textContent)
    expect(new Set(labels).size).toBe(labels.length)
    expect(labels.filter(label => label.startsWith('Strategy ('))).toEqual([
      'Strategy (adjust-none)', 'Strategy (adjust-ridge-logistic)', 'Strategy (variance-ew600)',
      'Strategy (variance-mad)', 'Strategy (custom-strategy)',
    ])
    expect(screen.getAllByRole('button', { name: 'Strategy', exact: true })).toHaveLength(5)
    for (const manifest of manifests.filter(manifest => manifest.title === 'Strategy')) {
      expect(within(entry(container, manifest.id)).getByRole('button', { name: 'Strategy', exact: true }))
        .toHaveAccessibleDescription(`${manifest.id}@1.0.0`)
    }
    search('VARIANCE')
    expect(addButtons(container).map(button => button.parentElement.dataset.libraryEntry)).toEqual(['variance-ew600', 'variance-mad'])
    search('Math')
    expect(addButtons(container)).toHaveLength(11)
    search('UNIQUE DESCRIPTION')
    expect(addButtons(container).map(button => button.textContent)).toEqual(['Kashbot configuration'])
    search('Observation')
    expect(addButtons(container)).toHaveLength(3)
  })
})

describe('library details', () => {
  it('opens from the info button, shows both honesty sentences and restores focus on close', async () => {
    render(<Palette entries={entriesOf(base)} onAdd={vi.fn()} />)
    const about = screen.getByRole('button', { name: 'About Strategy' })
    about.focus()
    fireEvent.click(about)
    const drawer = await screen.findByRole('dialog', { name: 'About Strategy' })
    const details = within(drawer)
    expect(details.getByRole('heading', { name: 'Declared sample, not a measurement' })).toBeVisible()
    expect(details.getByRole('heading', { name: 'Budget, not a measurement' })).toBeVisible()
    expect(details.getByText('p50 10 µs · p99 100 µs')).toBeVisible()
    expect(details.getByText(sentence('placeholder_block'))).toBeVisible()
    expect(details.getByText(sentence('runs_on_bot'))).toBeVisible()
    expect(within(details.getByRole('region', { name: 'Inputs' })).getByText('value: number · none · scalar')).toBeVisible()
    expect(within(details.getByRole('region', { name: 'Outputs' })).getByText('result: number · none · scalar')).toBeVisible()
    expect(details.getByRole('heading', { name: 'Input', exact: true }).nextElementSibling).toHaveTextContent('"value": 0')
    expect(details.getByRole('heading', { name: 'Output', exact: true }).nextElementSibling).toHaveTextContent('"result": 0')
    fireEvent.click(details.getByRole('button', { name: 'Close details' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(about).toHaveFocus()
  })

  it('shows browser-only wording, descriptions, setting types, bounds and registration constants', () => {
    const manifest = { ...count, description: 'Counts the supplied rows.', settings_schema: { properties: {
      limit: { type: 'integer', minimum: 0, maximum: 8 },
      label: { type: 'string', minLength: 1, maxLength: 12, const: '' },
    } } }
    const onClose = vi.fn()
    render(<LibraryDetails manifest={manifest} onClose={onClose} />)
    expect(screen.getByText('Counts the supplied rows.')).toBeVisible()
    expect(screen.getByText("Runs here as you edit. The Lab server's Run does not accept it yet.")).toBeVisible()
    expect(screen.queryByText(sentence('placeholder_block'))).not.toBeInTheDocument()
    expect(screen.queryByText(sentence('runs_on_bot'))).not.toBeInTheDocument()
    expect(screen.getByText(/^limit: integer/)).toHaveTextContent('minimum: 0 · maximum: 8')
    expect(screen.getByText(/^label: string/)).toHaveTextContent('minLength: 1 · maxLength: 12')
    expect(screen.getByText('· Set by registration. Operator only.')).toBeVisible()
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})

describe('recent blocks and placement', () => {
  it('reports arming and cancellation through the standalone palette callback without adding', () => {
    const onArm = vi.fn()
    const onAdd = vi.fn()
    render(<Palette entries={entriesOf(base)} onAdd={onAdd} onArm={onArm} />)
    const button = screen.getByRole('button', { name: 'Strategy' })
    tap(button)
    expect(onArm).toHaveBeenLastCalledWith(base)
    expect(button).toHaveAttribute('aria-pressed', 'true')
    tap(button)
    expect(onArm).toHaveBeenLastCalledWith(null)
    expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(onAdd).not.toHaveBeenCalled()
  })

  it('deduplicates newest first, caps at eight, and tolerates invalid or inaccessible storage', () => {
    pushRecent(localStorage, 'first'); pushRecent(localStorage, 'second'); pushRecent(localStorage, 'first')
    expect(readRecent(localStorage)).toEqual(['first', 'second'])
    for (let index = 0; index < 12; index += 1) pushRecent(localStorage, `block-${index}`)
    expect(readRecent(localStorage)).toEqual(['block-11', 'block-10', 'block-9', 'block-8', 'block-7', 'block-6', 'block-5', 'block-4'])
    for (const value of ['{', '{}', 'null', '[1]', '["valid", null]']) {
      localStorage.setItem(RECENT_KEY, value)
      expect(readRecent(localStorage)).toEqual([])
    }
    const blocked = { getItem() { throw Error('blocked') }, setItem() { throw Error('blocked') } }
    expect(readRecent(blocked)).toEqual([])
    expect(() => pushRecent(blocked, 'first')).not.toThrow()
    vi.stubGlobal('localStorage', blocked)
    render(<Palette entries={registry} onAdd={vi.fn()} />)
    expect(within(group('Recent')).getByText('No recent blocks.')).toBeVisible()
  })

  it('handles a denied localStorage property and still adds a block', async () => {
    vi.spyOn(globalThis, 'localStorage', 'get').mockImplementation(() => { throw Error('denied') })
    render(<LabCanvas entries={entriesOf(base)} />)
    await screen.findByText('Layout updated')
    expect(within(group('Recent')).getByText('No recent blocks.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Strategy' }))
    expect(screen.getAllByTestId('placed-block')).toHaveLength(1)
    expect(within(group('Recent')).getByText('No recent blocks.')).toBeVisible()
  })

  it('records real mouse and keyboard adds in recent order and retains centre placement', async () => {
    const { container } = render(<LabCanvas entries={entriesOf(base, count)} />)
    await screen.findByText('Layout updated')
    vi.spyOn(screen.getByTestId('pane').parentElement, 'getBoundingClientRect').mockReturnValue({ left: 100, top: 60, width: 600, height: 400 })
    fireEvent.pointerDown(within(entry(container, base.id)).getByRole('button', { name: 'Strategy' }), { pointerType: 'mouse' })
    fireEvent.click(within(entry(container, base.id)).getByRole('button', { name: 'Strategy' }))
    const button = within(entry(group('Math and data'), count.id)).getByRole('button', { name: 'Count' })
    fireEvent.keyDown(button, { key: 'Enter' })
    fireEvent.keyUp(button, { key: 'Enter' })
    fireEvent.click(button, { detail: 0 })
    expect(screen.getAllByTestId('placed-block')).toHaveLength(2)
    for (const node of screen.getAllByTestId('placed-block')) expect(node).toHaveAttribute('data-position', JSON.stringify({ x: 390, y: 240 }))
    expect(readRecent(localStorage)).toEqual(['count', 'adjust-none'])
    expect(addButtons(group('Recent')).map(button => button.parentElement.dataset.libraryEntry)).toEqual(['count', 'adjust-none'])
    expect(within(container).getAllByTestId(/^library-add-/)).toHaveLength(2)
    expect(within(group('Recent')).getAllByTestId(/^library-recent-add-/)).toHaveLength(2)
  })

  it.each(['touch', 'pen'])('arms %s, places exactly one block at the projected pane position, and clears the arm', async pointerType => {
    const changed = vi.fn()
    render(<LabCanvas entries={entriesOf(base)} onChange={changed} />)
    await screen.findByText('Layout updated')
    changed.mockClear()
    const button = within(group('Old Kashbot policy')).getByRole('button', { name: 'Strategy' })
    tap(button, pointerType)
    expect(button).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('Tap the canvas to place Strategy. Tap the entry again to cancel.')).toBeVisible()
    expect(screen.queryAllByTestId('placed-block')).toHaveLength(0)
    expect(readRecent(localStorage)).toEqual([])
    fireEvent.click(screen.getByTestId('pane'), { clientX: 210, clientY: 150 })
    expect(screen.getAllByTestId('placed-block')).toHaveLength(1)
    expect(screen.getByTestId('placed-block')).toHaveAttribute('data-position', JSON.stringify({ x: 200, y: 130 }))
    expect(changed).toHaveBeenCalledTimes(1)
    expect(changed.mock.calls[0][0].document.blocks).toHaveLength(1)
    expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByText(/Tap the canvas to place/)).not.toBeInTheDocument()
    expect(readRecent(localStorage)).toEqual(['adjust-none'])
    fireEvent.click(screen.getByTestId('pane'), { clientX: 300, clientY: 300 })
    expect(screen.getAllByTestId('placed-block')).toHaveLength(1)
  })

  it('cancels on a second tap or Escape, and a subsequent keyboard activation adds at centre', async () => {
    const { container } = render(<LabCanvas entries={entriesOf(base)} />)
    await screen.findByText('Layout updated')
    const button = screen.getByRole('button', { name: 'Strategy' })
    tap(button); tap(button)
    expect(button).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(screen.getByTestId('pane'), { clientX: 80, clientY: 90 })
    expect(screen.queryAllByTestId('placed-block')).toHaveLength(0)
    tap(button)
    fireEvent.keyDown(container.querySelector('[data-canvas-editor]'), { key: 'Escape' })
    expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByText(/Tap the canvas to place/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('pane'), { clientX: 80, clientY: 90 })
    expect(screen.queryAllByTestId('placed-block')).toHaveLength(0)
    fireEvent.keyDown(button, { key: 'Enter' })
    fireEvent.click(button, { detail: 0 })
    expect(screen.getAllByTestId('placed-block')).toHaveLength(1)
    expect(screen.getByTestId('placed-block')).toHaveAttribute('data-position', JSON.stringify({ x: -10, y: -20 }))
  })

  it('retains native drag/drop and records dropped blocks as recent', async () => {
    expect(BLOCK_MIME).toBe('application/leaf-lab-block')
    render(<LabCanvas entries={entriesOf(base)} />)
    await screen.findByText('Layout updated')
    const dataTransfer = { setData: vi.fn(), getData: vi.fn(() => base.id) }
    fireEvent.dragStart(screen.getByRole('button', { name: 'Strategy' }), { dataTransfer })
    expect(dataTransfer.setData).toHaveBeenCalledWith(BLOCK_MIME, base.id)
    expect(dataTransfer.effectAllowed).toBe('copy')
    // jsdom's fallback drop Event ignores the MouseEvent coordinate init fields.
    const drop = new MouseEvent('drop', { bubbles: true, cancelable: true, clientX: 120, clientY: 90 })
    Object.defineProperty(drop, 'dataTransfer', { value: dataTransfer })
    fireEvent(screen.getByTestId('pane').parentElement, drop)
    expect(screen.getByTestId('placed-block')).toHaveAttribute('data-position', JSON.stringify({ x: 110, y: 70 }))
    expect(readRecent(localStorage)).toEqual([base.id])
  })
})

describe('library standards', () => {
  it('uses glossary vocabulary in the palette and representative details', () => {
    const palette = render(<Palette entries={entries} onAdd={vi.fn()} />)
    expect(replacedWordsIn(palette.container.textContent)).toEqual([])
    palette.unmount()
    for (const manifest of [base, count, host]) {
      const view = render(<LibraryDetails manifest={manifest} onClose={vi.fn()} />)
      expect(replacedWordsIn(view.container.textContent)).toEqual([])
      view.unmount()
    }
  })
  it.each(['Palette.jsx', 'libraryCategories.js', 'LibraryDetails.jsx', 'LabCanvas.jsx'])('uses no literal colors in %s', file => {
    expect(colorLiteralsIn(readFileSync(new URL(file, import.meta.url), 'utf8'))).toEqual([])
  })
})
