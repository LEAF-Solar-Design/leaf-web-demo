// @vitest-environment jsdom
import { readFileSync, readdirSync } from 'node:fs'
import { relative, resolve } from 'node:path'
import { parse } from '@babel/parser'
import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import coverage from './coverage.json'
import EscCap from '../components/EscCap.jsx'
import SchemaForm from '../components/SchemaForm.jsx'
import ResultPanel from '../components/ResultPanel.jsx'
import ShortcutSheet from '../components/ShortcutSheet.jsx'
import CockpitIcon from '../site/CockpitIcon.jsx'
import SheetFrame, { scrollToSheet } from '../site/sheets/SheetFrame.jsx'
import CodeTab from '../lab/code/CodeTab.jsx'
import { manifests } from '../lab/canvas/registry.js'
import { keyboardTable } from '../lib/actionRegistry.js'
import { SINGLE_KEY_SHORTCUTS_KEY } from '../lib/singleKeyPreference.js'

vi.mock('../lib/actionRegistry.js', () => ({ keyboardTable: vi.fn() }))
vi.mock('../site/router.js', () => ({ navigate: vi.fn() }))

// Vitest runs from web/; jsdom import.meta.url is not a file URL.
const sourceDirectory = resolve(process.cwd(), 'src')
const read = path => readFileSync(resolve(sourceDirectory, path), 'utf8')
const standard = read('standard/leaf-standard.css')
const foundation = read('standard/parity-foundation.css')
const verified = coverage.filter(row => row.status === 'verified')
const executed = []
const completed = []
const styles = []

// Contract citations from amendment 2, with amendment 3 assigning W2-06
// commercial copy to DP-A23. Other uncited checks belong to this slice.
const sliceRules = {
  'DP-W1-01': [], 'DP-W1-02': [], 'DP-W1-03': [11], 'DP-W1-04': [],
  'DP-W1-05': [], 'DP-W1-06': [20], 'DP-W1-07': [], 'DP-W1-08': [],
  'DP-W1-09': [25], 'DP-W1-10': [], 'DP-W1-11': [], 'DP-W1-12': [10],
  'DP-W1-13': [11], 'DP-W1-14': [13], 'DP-W1-15': [5], 'DP-W1-16': [],
  'DP-W1-17': [1, 22], 'DP-W1-18': [23], 'DP-W1-19': [15, 16],
  'DP-W1-20': [16], 'DP-W1-21': [14], 'DP-W1-22': [], 'DP-W1-23': [19, 20],
  'DP-W2-01': [18], 'DP-W2-02': [18, 23], 'DP-W2-03': [15],
  'DP-W2-04': [16], 'DP-W2-05': [17], 'DP-W2-06': [23], 'DP-W2-07': [],
  'DP-W2-08': [], 'DP-W2-09': [], 'DP-W2-10': [], 'DP-W2-11': [22],
  'DP-W2-12': [21, 22], 'DP-W2-13': [11],
}

function inject(css) {
  const style = document.createElement('style')
  style.textContent = css
  document.head.append(style)
  styles.push(style)
  return style
}

function inheritedToken(element, name) {
  for (let node = element; node; node = node.parentElement) {
    const value = getComputedStyle(node).getPropertyValue(name).trim()
    if (value) return value
  }
  throw new Error(`Missing token ${name}`)
}

function expectTokens(expected) {
  for (const [name, value] of Object.entries(expected)) {
    expect(getComputedStyle(document.documentElement).getPropertyValue(name).trim(), name).toBe(value)
  }
}

function resolveVars(value, element, depth = 0) {
  if (depth > 10) throw new Error(`Cyclic token in ${value}`)
  return value.replace(/var\((--[\w-]+)\)/g, (_, name) =>
    resolveVars(inheritedToken(element, name), element, depth + 1))
}

// jsdom 25 does not resolve var() or expand outline shorthand. Materialize
// only matching production declarations, retaining selectors and cascade order.
// No expected color or width is supplied to this compatibility helper.
function materialize(style, element, properties) {
  for (const rule of style.sheet.cssRules) {
    if (!rule.selectorText || !element.matches(rule.selectorText)) continue
    for (const property of properties) {
      const value = rule.style.getPropertyValue(property)
      if (!value) continue
      const resolved = resolveVars(value, element)
      rule.style.setProperty(property, resolved)
      if (property === 'outline') {
        const width = resolved.match(/(?:^|\s)(\d+(?:\.\d+)?px)(?=\s|$)/)?.[1]
        if (!width) throw new Error(`Unrecognized outline: ${resolved}`)
        rule.style.setProperty('outline-width', width)
      }
    }
  }
  style.textContent = Array.from(style.sheet.cssRules, rule => rule.cssText).join('\n')
}

const shortcutRows = [
  { id: 'acceptance:command', label: 'Focus command', kbd: 'Mod+K', context: 'Anywhere', singleKey: false },
  { id: 'acceptance:retry', label: 'Retry', kbd: 'R', context: 'Outside text fields', singleKey: true },
]

function Form() {
  const [values, setValues] = useState({ name: 'Roof', width: 2 })
  return <form aria-label="Parameters" onSubmit={event => event.preventDefault()}>
    <SchemaForm schema={{ required: ['name'], properties: {
      name: { type: 'string', description: 'Name this roof.' },
      width: { type: 'number', 'x-leaf-unit': 'mm', minimum: 1 },
    } }} values={values} onChange={setValues} />
  </form>
}

function resultView() {
  return render(<ResultPanel result={{ ok: true, tool: 'acceptance', version: '1.0.0', timing_ms: 1,
    result: { table: { columns: ['name', 'count'], rows: [['Panels', '1,204']] }, count: 1204, name: 'Panels' },
  }} />)
}

function codeView() {
  return render(<CodeTab block={{ id: 'adjust-none-acceptance', settings: {} }}
    manifest={manifests.find(item => item.id === 'adjust-none')} />)
}

function openShortcuts() {
  keyboardTable.mockReturnValue(shortcutRows)
  render(<ShortcutSheet open onClose={() => {}} />)
  return screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
}

function jsxFiles(directory) {
  return readdirSync(directory, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))
    .flatMap(entry => {
      const path = resolve(directory, entry.name)
      return entry.isDirectory() ? jsxFiles(path) : entry.name.endsWith('.jsx') ? [path] : []
    })
}

const copyFiles = () => ['components/', 'site/'].flatMap(path => jsxFiles(resolve(sourceDirectory, path)))

// Amendment 3 permits only these exact pre-existing file, line and text hits.
const knownDashHits = new Set([
  'web/src/components/operator/AuditPanel.jsx:55: —',
  'web/src/site/DrawingCockpit.jsx:615: —',
  'web/src/site/DrawingCockpit.jsx:616: —',
  'web/src/site/DrawingCockpit.jsx:617: —',
])

function dashHits(node, file, hits) {
  if (!node || typeof node !== 'object') return
  if (node.type === 'JSXText' && /[\u2013\u2014]/u.test(node.value)) {
    node.value.split('\n').forEach((line, offset) => {
      if (/[\u2013\u2014]/u.test(line)) hits.push(`${file}:${node.loc.start.line + offset}: ${line.trim()}`)
    })
  }
  for (const [key, child] of Object.entries(node)) {
    if (key === 'loc' || key === 'tokens' || key === 'comments') continue
    if (Array.isArray(child)) child.forEach(value => dashHits(value, file, hits))
    else if (child && typeof child === 'object') dashHits(child, file, hits)
  }
}

const assertions = {
  'tokens retain 129 root declarations and Stage and Paper control lines': () => {
    const rootSource = standard.match(/:root\s*\{([^}]*)\}/)?.[1]
    expect(rootSource).toBeDefined()
    expect([...rootSource.matchAll(/--leaf-[\w-]+\s*:/g)]).toHaveLength(129)
    const rootRule = styles[0].sheet.cssRules[0]
    expect(rootRule.selectorText).toBe(':root')
    expect(Array.from({ length: rootRule.style.length }, (_, index) => rootRule.style[index])
      .filter(name => name.startsWith('--leaf-'))).toHaveLength(129)
    delete document.documentElement.dataset.leafTheme
    expectTokens({ '--leaf-line-control': '#737e76' })
    for (const [theme, expected] of [['stage', '#737e76'], ['paper', '#737b71']]) {
      document.documentElement.dataset.leafTheme = theme
      expect(getComputedStyle(document.documentElement).getPropertyValue('--leaf-line-control').trim()).toBe(expected)
    }
  },
  'spacing tokens preserve the extended spacing scale': () => {
    expectTokens({ '--leaf-space-9': '96px', '--leaf-space-10': '128px' })
  },
  'container tokens preserve prose width and responsive gutters': () => {
    expectTokens({ '--leaf-metric-container': '1280px', '--leaf-metric-prose': '720px',
      '--leaf-metric-gutter': '24px', '--leaf-metric-gutter-compact': '16px' })
  },
  'radius tokens distinguish fields and command entry': () => {
    expectTokens({ '--leaf-radius-field': '7px', '--leaf-radius-command': '11px' })
  },
  'Stage elevation tokens preserve dialog showcase and popover shadows': () => {
    expectTokens({ '--leaf-shadow-dialog': '0 16px 48px rgba(0,0,0,.72)',
      '--leaf-shadow-showcase': '0 24px 64px -16px rgba(0,0,0,.6)',
      '--leaf-shadow-popover': '0 8px 24px rgba(0, 0, 0, 0.7)' })
  },
  'Stage panel tokens distinguish surface raised and border roles': () => {
    expectTokens({ '--leaf-surface': '#0e110f', '--leaf-raised': '#161a17',
      '--leaf-line': '#262a27', '--leaf-line-control': '#737e76' })
  },
  'touch tokens preserve coarse hit targets and readable input text': () => {
    expectTokens({ '--leaf-metric-hit-fine': '24px', '--leaf-metric-hit-coarse': '44px',
      '--leaf-metric-control-touch': '44px', '--leaf-size-touch': '16px' })
  },
  'ghost buttons have a transparent background': () => {
    render(<button className="btn ghost">Secondary action</button>)
    expect(['transparent', 'rgba(0, 0, 0, 0)']).toContain(
      getComputedStyle(screen.getByRole('button')).backgroundColor)
  },
  'button keyboard focus uses a 2px outline': () => {
    const style = inject(foundation.replaceAll(':focus-visible', '.acceptance-focus-visible'))
    render(<button className="btn acceptance-focus-visible">Keyboard action</button>)
    const button = screen.getByRole('button')
    button.focus()
    expect(document.activeElement).toBe(button)
    expect(button.matches('.acceptance-focus-visible')).toBe(true)
    materialize(style, button, ['outline'])
    expect(getComputedStyle(button).outlineWidth).toBe('2px')
  },
  'EscCap defaults to a non-submitting button': () => {
    const submit = vi.fn(event => event.preventDefault())
    const close = vi.fn()
    render(<form onSubmit={submit}><EscCap onClick={close} /></form>)
    const button = screen.getByRole('button', { name: 'Close' })
    expect(button.type).toBe('button')
    fireEvent.click(button)
    expect(close).toHaveBeenCalledTimes(1)
    expect(submit).not.toHaveBeenCalled()
  },
  'SchemaForm merges help and error descriptions': () => {
    render(<Form />)
    const name = screen.getByLabelText('Name', { exact: true })
    expect(name.getAttribute('aria-describedby')).toBe('sf-name-help')
    fireEvent.change(name, { target: { value: '' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Parameters' }))
    expect(name.getAttribute('aria-describedby')).toBe('sf-name-help sf-name-error')
    expect(document.getElementById('sf-name-help').textContent).toBe('Name this roof.')
    expect(document.getElementById('sf-name-error').getAttribute('role')).toBe('alert')
  },
  'SchemaForm unit suffixes remain decorative': () => {
    render(<Form />)
    const suffix = screen.getByLabelText('Width').closest('[data-dp-field]').querySelector('.sf-unit')
    expect(suffix.textContent).toBe('mm')
    expect(suffix.getAttribute('aria-hidden')).toBe('true')
  },
  'ResultPanel gives column and row headers scope': () => {
    const { container } = resultView()
    const columns = container.querySelectorAll('.grid thead th')
    expect(columns).toHaveLength(2)
    columns.forEach(header => expect(header.getAttribute('scope')).toBe('col'))
    const rows = container.querySelectorAll('.kv th')
    expect(rows).toHaveLength(2)
    rows.forEach(header => expect(header.getAttribute('scope')).toBe('row'))
  },
  'ResultPanel assigns dp-number only to numeric cells': () => {
    const { container } = resultView()
    const cells = container.querySelector('.grid tbody tr').cells
    expect(cells[0].classList.contains('dp-number')).toBe(false)
    expect(cells[1].classList.contains('dp-number')).toBe(true)
    expect(container.querySelector('.kv td').classList.contains('dp-number')).toBe(true)
  },
  'Lab Studio adapter resolves its background to Stage ground': () => {
    inject(read('lab/studioParityAdapter.css'))
    const { container } = render(<div data-leaf-profile="studio"><section className="lab-workbench" /></div>)
    const workbench = container.querySelector('.lab-workbench')
    const background = getComputedStyle(workbench).getPropertyValue('--leaf-color-background').trim()
    expect(background).toBe('var(--leaf-ground)')
    expect(resolveVars(background, workbench)).toBe('#050706')
  },
  'CodeTab shows Copied only after clipboard resolution': async () => {
    vi.useFakeTimers()
    let resolveCopy
    const pending = new Promise(resolve => { resolveCopy = resolve })
    const writeText = vi.fn(() => pending)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    codeView()
    const button = screen.getByRole('button', { name: 'Copy export-contract', exact: true })
    fireEvent.click(button)
    expect(writeText).toHaveBeenCalledExactlyOnceWith(button.closest('.dp-code-block').querySelector('code').textContent)
    expect(button.textContent).toBe('Copy')
    act(() => vi.advanceTimersByTime(2000))
    expect(button.textContent).toBe('Copy')
    await act(async () => { resolveCopy() })
    expect(button.textContent).toBe('Copied')
    act(() => vi.advanceTimersByTime(2000))
    expect(button.textContent).toBe('Copy')
  },
  'CodeTab renders code headers with the data typography recipe': () => {
    const { container } = codeView()
    const blocks = container.querySelectorAll('.dp-code-block')
    expect(blocks.length).toBeGreaterThan(0)
    blocks.forEach(block => {
      expect(block.querySelector('.dp-code-header button')).not.toBeNull()
      expect(block.querySelector('pre code')).not.toBeNull()
    })
    const css = read('lab/code/codeParity.css')
    for (const declaration of ['font-family:var(--leaf-font-data)', 'font-size:var(--leaf-size-dense)',
      'line-height:var(--leaf-leading-code)', 'font-variant-ligatures:none', 'overflow-x:auto', 'white-space:pre']) {
      expect(css).toContain(declaration)
    }
  },
  'ShortcutSheet renders registry shortcuts in two groups': () => {
    const dialog = openShortcuts()
    expect(within(dialog).getAllByRole('heading').map(node => node.textContent)).toEqual(['Shortcuts', 'Single keys'])
    expect(within(dialog).getAllByTestId('shortcut-row')).toHaveLength(shortcutRows.length)
    expect(within(screen.getByRole('region', { name: 'Shortcuts' })).getByText('Focus command')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: 'Single keys' })).getByText('Retry')).toBeTruthy()
    expect(Array.from(dialog.querySelectorAll('kbd'), node => node.textContent)).toEqual(['Mod', 'K', 'R'])
  },
  'ShortcutSheet disables single keys independently of modifier shortcuts': () => {
    openShortcuts()
    fireEvent.click(screen.getByRole('switch', { name: 'Single-key shortcuts' }))
    expect(localStorage.getItem(SINGLE_KEY_SHORTCUTS_KEY)).toBe('off')
    for (const row of shortcutRows) {
      expect(screen.getByText(row.label).closest('[data-shortcut-id]').getAttribute('aria-disabled'))
        .toBe(row.singleKey ? 'true' : null)
    }
  },
  'CockpitIcon hides decorative sprites from assistive technology': () => {
    const { container } = render(<CockpitIcon id="line" label="Ignored decorative label" />)
    const icon = container.querySelector('svg')
    expect(icon.getAttribute('aria-hidden')).toBe('true')
    expect(icon.getAttribute('focusable')).toBe('false')
    expect(icon.hasAttribute('aria-label')).toBe(false)
    expect(screen.queryByRole('img')).toBeNull()
    expect(container.querySelector('use').getAttribute('href')).toMatch(/#i8-line$/)
  },
  'component and site JSX contains no obsolete Leaf branding': () => {
    const files = copyFiles()
    expect(files.length).toBeGreaterThan(0)
    const hits = files.flatMap(file => readFileSync(file, 'utf8').split('\n').flatMap((line, index) =>
      /Leaf automation|LeafAutomation/.test(line) ? [`${file}:${index + 1}: ${line.trim()}`] : []))
    expect(hits).toEqual([])
  },
  'component and site JSX text contains no en or em dashes': () => {
    const files = copyFiles()
    expect(files.length).toBeGreaterThan(0)
    const hits = []
    for (const file of files) {
      const ast = parse(readFileSync(file, 'utf8'), { sourceType: 'module', plugins: ['jsx'] })
      const relativeFile = `web/src/${relative(sourceDirectory, file).replaceAll('\\', '/')}`
      dashHits(ast, relativeFile, hits)
    }
    expect(knownDashHits.size).toBe(4)
    expect(hits.filter(hit => !knownDashHits.has(hit))).toEqual([])
  },
  'SheetFrame uses auto scrolling for reduced motion': () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true })))
    const scroll = vi.fn()
    render(<SheetFrame id="acceptance-sheet" titleBlock={{ sheetCode: 'A-101', sheetTitle: 'Acceptance' }} />)
    const sheet = document.getElementById('acceptance-sheet')
    sheet.scrollIntoView = scroll
    scrollToSheet(sheet.id)
    expect(matchMedia).toHaveBeenCalledWith('(prefers-reduced-motion: reduce)')
    expect(scroll).toHaveBeenCalledExactlyOnceWith({ behavior: 'auto', block: 'start' })
  },
  'Ops workspace uses the standard text font': () => {
    const css = read('integrations/ops-workspace.css')
    expect(css).toContain('font-family: var(--leaf-font-text)')
    expect(css).not.toContain('system-ui')
  },
  'Sheets hint ink computes to rgb(161, 161, 170)': () => {
    const style = inject(read('site/sheets.css'))
    render(<main className="sheets-root"><SheetFrame id="hint-sheet"
      titleBlock={{ sheetCode: 'A-101', sheetTitle: 'Acceptance' }}
      left={<div className="sheet-index-row"><span className="hint">Sheet hint</span></div>} /></main>)
    const hint = screen.getByText('Sheet hint')
    expect(inheritedToken(hint, '--leaf-ink-muted')).toBe('#a1a1aa')
    materialize(style, hint, ['color'])
    expect(getComputedStyle(hint).color).toBe('rgb(161, 161, 170)')
  },
}

beforeAll(() => {
  expect(new Set(coverage.map(row => row.rule))).toEqual(new Set(Array.from({ length: 25 }, (_, index) => `DP-A${index + 1}`)))
  expect(new Set(coverage.map(row => JSON.stringify([row.rule, row.slice, row.assertion]))).size).toBe(coverage.length)
  expect(new Set(verified.map(row => row.assertion)).size).toBe(verified.length)
  const gates = ['F-SHELL', 'F-SHELL', 'F-SHELL+C2', 'F-SHELL', 'C3', 'F-COMM']
  gates.forEach((gate, index) => {
    const slice = `DP-W2-${String(index + 1).padStart(2, '0')}`
    const rows = coverage.filter(row => row.slice === slice)
    // W2-02 needs both cited rules; W2-06 uses amendment 3's copy rule.
    expect(new Set(rows.map(row => row.rule))).toEqual(new Set(sliceRules[slice].map(rule => `DP-A${rule}`)))
    rows.forEach(row => {
      // A promoted row is allowed only with an implementation, checked below.
      if (row.status === 'deferred') expect(row.fork).toBe(gate)
    })
  })
  for (const row of coverage) {
    expect(Object.keys(row).sort()).toEqual(['assertion', 'fork', 'rule', 'slice', 'status'])
    expect(row.slice).toMatch(/^DP-W[123]-\d{2}$/)
    if (row.slice === 'DP-W3-02') {
      expect(['DP-A21', 'DP-A22']).toContain(row.rule)
      expect(row.status).toBe('deferred')
      expect(row.fork).toBe('website-only')
    } else if (row.slice !== 'DP-W3-01') {
      expect(['DP-W2-11', 'DP-W2-12']).not.toContain(row.slice)
      expect(sliceRules[row.slice], `Unknown contract slice: ${row.slice}`).toBeDefined()
      expect(sliceRules[row.slice].map(rule => `DP-A${rule}`), `Uncited rule for ${row.slice}`).toContain(row.rule)
    }
    expect(typeof row.assertion).toBe('string')
    expect(row.assertion.trim().length).toBeGreaterThan(0)
    expect(['verified', 'deferred']).toContain(row.status)
    if (row.status === 'verified') {
      expect(row.fork).toBeNull()
      expect(typeof assertions[row.assertion], `Missing implementation: ${row.assertion}`).toBe('function')
    } else {
      expect(typeof row.fork).toBe('string')
      expect(row.fork.trim().length).toBeGreaterThan(0)
    }
  }
})

beforeEach(() => {
  document.documentElement.dataset.leafTheme = 'stage'
  localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY)
  inject(standard)
  inject(foundation)
})

afterEach(() => {
  cleanup()
  styles.splice(0).forEach(style => style.remove())
  delete document.documentElement.dataset.leafTheme
  localStorage.removeItem(SINGLE_KEY_SHORTCUTS_KEY)
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

// Deferred rows are data, never registered as skipped tests. Missing promoted
// implementations fail the schema guard and their own row instead of disappearing.
for (const row of verified) {
  test(row.assertion, async () => {
    executed.push(row.assertion)
    await assertions[row.assertion]()
    completed.push(row.assertion)
  })
}

afterAll(() => {
  expect(executed).toHaveLength(verified.length)
  expect(completed).toHaveLength(verified.length)
  expect([...executed].sort()).toEqual(verified.map(row => row.assertion).sort())
  for (const row of coverage.filter(row => row.status === 'deferred')) {
    console.info(`DEFERRED ${row.rule} ${row.slice} gate=${row.fork}: ${row.assertion}`)
  }
})
