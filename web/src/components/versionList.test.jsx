/**
 * THE ONE version-list primitive (standardization slice 6a).
 *
 * Two claims are under test, and the first is the one that makes the refactor
 * safe to merge:
 *
 *  1. BYTE-IDENTICAL /app DRAWER DOM. The literal below was CAPTURED from the
 *     pre-slice VersionHistory.jsx (origin/main ae5c01bf) by rendering it with
 *     this exact fixture and printing `container.innerHTML`. It is the ORACLE:
 *     it is not derived from the component under test, so a refactor that
 *     moves a span, drops a class, adds a `type` attribute or reorders an
 *     attribute fails here rather than reaching an e2e selector. Only two
 *     substrings are normalized, and only because they are host-locale and
 *     host-timezone derived: the row's absolute-time `title` and the relative
 *     `.vh-when` text. Every tag, class, attribute, testid and their ORDER are
 *     pinned as they shipped.
 *
 *  2. The primitive's own behaviour: newest-first ordering, both skins, the
 *     two-step confirm state machine, single-flight restore, the fail-closed
 *     row filter, and the provenance chip's render-only-when-present rule.
 *
 * Run:  cd web && npx vitest run src/components/versionList.test.jsx
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import VersionHistory from './VersionHistory.jsx'
import VersionList, { SourceRefChip, VersionPreviewStrip } from './VersionList.jsx'

// This workspace's vitest setup registers jest-dom matchers only, so nothing
// unmounts a render between cases. Several specs below render the SAME testid
// on both skins; without an explicit teardown they would query a document
// holding every previous render and fail on ambiguity rather than on truth.
afterEach(cleanup)

// Fixed 2020 dates so `fmtWhen` always takes its absolute branch: a fixture
// dated "now" would flip between "2 m" and a date and make the pin a clock.
const CHAIN = [
  { v: 1, parent: null, created: '2020-03-01T12:00:00Z', bytes: 10, sha256: 'aaaaaaaaaaaaaaaa', tool: null, workitem_id: null, note: null, delta: null, source_ref: null },
  { v: 2, parent: 1, created: '2020-03-02T12:00:00Z', bytes: 20, sha256: 'bbbbbbbbbbbbbbbb', tool: 'drawing.write', workitem_id: 'wi', note: 'second', delta: { added: 1, modified: 2, deleted: 0 }, source_ref: null },
  { v: 3, parent: 2, created: '2020-03-03T12:00:00Z', bytes: 30, sha256: 'cccccccccccccccc', tool: 'authored.tool', workitem_id: null, note: 'third', delta: { added: 0, modified: 0, deleted: 0 }, source_ref: null },
]
const DATA = { drawing_id: 'demo', head: 3, latest: 3, versions: CHAIN }

const RECEIPT = '9f'.repeat(32) // 64 lowercase hex, the shape the server ships

// Captured from the PRE-SLICE component. Do not regenerate from the current
// component: that would make the oracle agree with any future change.
const DRAWER_DOM_BEFORE_THE_SLICE =
  '<div class="drawer drawer-fixed" role="dialog" aria-label="Version history" data-escape-owner="true"><div class="drawer-head"><span class="drawer-title">Version history · 3</span><button class="key hot" aria-label="Close version history">Esc</button></div><div class="drawer-body"><div class="vh-previewing"><span>Viewing v2 of 3 — read-only preview</span><button class="chip-act">Back to head</button></div><ul class="vh-list"><li data-testid="vh-row-v3"><div class="vh-row-line"><button class="vh-row" title="§ABS§"><span class="lbar"></span><span class="vh-main"><span class="vh-row-top"><span class="vh-v">v3</span><span class="vh-tool">authored.tool</span><span class="vh-mark">head</span><span class="vh-delta vh-delta-none" data-testid="vh-delta" title="No entity changes from the parent version">±0</span></span><span class="vh-row-sub"><span class="vh-note-txt">third</span><span class="drawer-mono">cccccccccccc</span><span class="vh-when">§WHEN§</span></span></span></button></div></li><li data-testid="vh-row-v2"><div class="vh-row-line"><button class="vh-row active" title="§ABS§"><span class="lbar"></span><span class="vh-main"><span class="vh-row-top"><span class="vh-v">v2</span><span class="vh-tool">drawing.write</span><span class="vh-delta" data-testid="vh-delta" title="1 added · 2 modified · 0 removed (vs. the parent version)"><span class="vh-delta-add">+1</span><span class="vh-delta-mod">~2</span></span></span><span class="vh-row-sub"><span class="vh-note-txt">second</span><span class="drawer-mono">bbbbbbbbbbbb</span><span class="vh-when">§WHEN§</span></span></span><span class="key hot">Enter</span></button><span class="vh-restore" aria-label="Restore version 2"><button class="chip-act">Restore</button></span></div></li><li data-testid="vh-row-v1"><div class="vh-row-line"><button class="vh-row" title="§ABS§"><span class="lbar"></span><span class="vh-main"><span class="vh-row-top"><span class="vh-v">v1</span></span><span class="vh-row-sub"><span class="drawer-mono">aaaaaaaaaaaa</span><span class="vh-when">§WHEN§</span></span></span></button><span class="vh-restore" aria-label="Restore version 1"><button class="chip-act">Restore</button></span></div></li></ul></div></div>'

// Locale/timezone-derived text only. Deliberately narrow patterns: they cannot
// swallow a class, a testid or an element.
function normalize(html) {
  return html
    .replace(/title="[A-Z][a-z]{2} \d+, \d{2}:\d{2} [AP]M"/g, 'title="§ABS§"')
    .replace(/<span class="vh-when">[^<]*<\/span>/g, '<span class="vh-when">§WHEN§</span>')
}

function renderDrawer(props = {}) {
  return render(
    <VersionHistory
      data={DATA}
      error={null}
      loading={false}
      previewingVersion={2}
      onPreview={() => {}}
      onBackToHead={() => {}}
      onClose={() => {}}
      onRetry={() => {}}
      retryKey={false}
      exiting={false}
      mock
      capability={null}
      onRestored={() => {}}
      headWarning={null}
      mutationBlocked={false}
      {...props}
    />,
  )
}

describe('/app drawer: the DOM did not move', () => {
  it('renders the element sequence captured from the pre-slice component, byte for byte', () => {
    const { container } = renderDrawer()
    expect(normalize(container.innerHTML)).toBe(DRAWER_DOM_BEFORE_THE_SLICE)
  })

  it('is not vacuous: the pin catches a single moved attribute', () => {
    // Without this, a normalizer that over-matched would report GREEN forever.
    const mutated = DRAWER_DOM_BEFORE_THE_SLICE.replace('class="vh-row active"', 'class="vh-row"')
    expect(normalize(mutated)).not.toBe(DRAWER_DOM_BEFORE_THE_SLICE)
    const retyped = DRAWER_DOM_BEFORE_THE_SLICE.replace('<button class="chip-act">Restore', '<button type="button" class="chip-act">Restore')
    expect(normalize(retyped)).not.toBe(DRAWER_DOM_BEFORE_THE_SLICE)
  })

  it('keeps every row testid the e2e selectors address', () => {
    renderDrawer()
    for (const v of [1, 2, 3]) expect(screen.getByTestId(`vh-row-v${v}`)).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// Provenance chip
// ---------------------------------------------------------------------------
describe('authored-tool provenance chip', () => {
  it('renders only on rows that carry a source_ref, on BOTH skins', () => {
    const withRef = CHAIN.map((r) => (r.v === 2 ? { ...r, source_ref: RECEIPT } : r))
    for (const variant of ['drawer', 'tab']) {
      const { container, unmount } = render(
        <VersionList variant={variant} versions={withRef} head={3} onPreview={() => {}} />,
      )
      const chips = container.querySelectorAll('[data-testid="vh-source-ref"]')
      expect(chips.length).toBe(1)
      expect(chips[0].textContent).toBe(`authored by Claude · ${RECEIPT.slice(0, 8)}`)
      // The full digest rides the title; only 8 characters are shown.
      expect(chips[0].getAttribute('title')).toBe(`Authored tool receipt ${RECEIPT}`)
      unmount()
    }
  })

  it('renders nothing for an absent, empty or non-string ref — never an invented author', () => {
    for (const bad of [null, undefined, '', 0, {}, []]) {
      const { container, unmount } = render(<SourceRefChip sourceRef={bad} />)
      expect(container.innerHTML).toBe('')
      unmount()
    }
  })

  it('is display-only: no anchor, no href', () => {
    const { container } = render(<SourceRefChip sourceRef={RECEIPT} />)
    expect(container.querySelector('a')).toBe(null)
    expect(container.innerHTML).not.toContain('href')
  })
})

// ---------------------------------------------------------------------------
// Ordering, fail-closed rows, and delta parity across the two shells
// ---------------------------------------------------------------------------
describe('the primitive itself', () => {
  it('orders newest first even when the chain arrives unordered', () => {
    const shuffled = [CHAIN[1], CHAIN[2], CHAIN[0]]
    const { container } = render(
      <VersionList variant="tab" versions={shuffled} head={3} onPreview={() => {}} />,
    )
    const ids = [...container.querySelectorAll('[data-testid^="try-version-v"]')]
      .map((el) => el.getAttribute('data-testid'))
    expect(ids).toEqual(['try-version-v3', 'try-version-v2', 'try-version-v1'])
  })

  it('fails closed on an unusable row rather than rendering a NaN testid', () => {
    const dirty = [...CHAIN, { v: 'not-a-number' }, null, { v: undefined }]
    const { container } = render(
      <VersionList variant="tab" versions={dirty} head={3} onPreview={() => {}} />,
    )
    expect(container.querySelectorAll('[data-testid^="try-version-v"]').length).toBe(3)
    expect(container.innerHTML).not.toContain('NaN')
  })

  it('survives a non-array payload', () => {
    for (const bad of [null, undefined, 'versions', { versions: [] }]) {
      const { container, unmount } = render(
        <VersionList variant="drawer" versions={bad} head={1} onPreview={() => {}} />,
      )
      expect(container.querySelector('.vh-list').children.length).toBe(0)
      unmount()
    }
  })

  it('/try now shows the SAME delta chips /app shows — the parity this slice buys', () => {
    const { container } = render(
      <VersionList variant="tab" versions={CHAIN} head={3} onPreview={() => {}} />,
    )
    // v2 (+1 ~2) and v3 (±0) both chip; v1 is the root and carries none.
    expect(container.querySelectorAll('[data-testid="vh-delta"]').length).toBe(2)
  })

  it('previewing a row calls back with that version', () => {
    const onPreview = vi.fn()
    render(<VersionList variant="tab" versions={CHAIN} head={3} onPreview={onPreview} />)
    fireEvent.click(screen.getByTestId('try-version-v2').querySelector('button'))
    expect(onPreview).toHaveBeenCalledWith(2)
  })
})

// ---------------------------------------------------------------------------
// The two-step confirm state machine, owned once
// ---------------------------------------------------------------------------
describe('two-step restore, one implementation', () => {
  function restoreProps(run, extra = {}) {
    return {
      run,
      mode: 'restore',
      eligible: (_row, isHead) => !isHead,
      disabled: false,
      ...extra,
    }
  }

  it('needs two presses, and only then runs the effect', () => {
    const run = vi.fn().mockResolvedValue(undefined)
    render(<VersionList variant="drawer" versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run)} />)
    const row = screen.getByTestId('vh-row-v2')
    fireEvent.click(row.querySelector('.vh-restore button'))
    expect(run).not.toHaveBeenCalled()
    expect(row.textContent).toContain('Restore v2 as the new head?')
    fireEvent.click(row.querySelectorAll('.vh-restore button')[0])
    expect(run).toHaveBeenCalledWith(2)
  })

  it('is single-flight: a second row cannot start while one is running', async () => {
    let release
    const run = vi.fn(() => new Promise((resolve) => { release = resolve }))
    render(<VersionList variant="drawer" versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run)} />)
    const v2 = screen.getByTestId('vh-row-v2')
    fireEvent.click(v2.querySelector('.vh-restore button'))
    fireEvent.click(v2.querySelectorAll('.vh-restore button')[0])
    expect(run).toHaveBeenCalledTimes(1)
    // Every other row's action is disabled while the first is in flight.
    const v1 = screen.getByTestId('vh-row-v1')
    expect(v1.querySelector('.vh-restore button').disabled).toBe(true)
    await act(async () => { release() })
    expect(run).toHaveBeenCalledTimes(1)
  })

  it('surfaces a rejected restore on the row and leaves the list usable', async () => {
    const run = vi.fn().mockRejectedValue(new Error('checkout is held by another session'))
    render(<VersionList variant="drawer" versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run)} />)
    const row = screen.getByTestId('vh-row-v2')
    fireEvent.click(row.querySelector('.vh-restore button'))
    await act(async () => { fireEvent.click(row.querySelectorAll('.vh-restore button')[0]) })
    expect(screen.getByRole('alert').textContent).toBe('checkout is held by another session')
    expect(screen.getByTestId('vh-row-v1').querySelector('.vh-restore button').disabled).toBe(false)
  })

  it('honours `disabled` (a blocked mutation) without hiding the affordance', () => {
    const run = vi.fn()
    render(<VersionList variant="drawer" versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run, { disabled: true })} />)
    const button = screen.getByTestId('vh-row-v2').querySelector('.vh-restore button')
    expect(button.disabled).toBe(true)
  })

  it('labels recovery mode the way both shells did: "Recover" then "Recover from vN"', () => {
    const run = vi.fn()
    render(<VersionList variant="tab" versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run, { mode: 'recover' })} />)
    const row = screen.getByTestId('try-version-v2')
    const trigger = [...row.querySelectorAll('button')].find((b) => b.textContent === 'Recover')
    expect(trigger).toBeTruthy()
    fireEvent.click(trigger)
    expect(row.textContent).toContain('Recover from v2')
    expect(row.querySelector('.tc-version-recovery')).toBeTruthy()
  })

  it('never offers the action on the head row, on either skin', () => {
    const run = vi.fn()
    for (const [variant, prefix] of [['drawer', 'vh-row-v'], ['tab', 'try-version-v']]) {
      const { unmount } = render(
        <VersionList variant={variant} versions={CHAIN} head={3} onPreview={() => {}} restore={restoreProps(run)} />,
      )
      expect(screen.getByTestId(`${prefix}3`).querySelectorAll('.chip-act').length).toBe(0)
      unmount()
    }
  })

  it('renders no action at all for a shell that passes none', () => {
    const { container } = render(<VersionList variant="tab" versions={CHAIN} head={3} onPreview={() => {}} />)
    expect(container.querySelectorAll('.chip-act').length).toBe(0)
  })
})

// ---------------------------------------------------------------------------
// The preview strip: one behaviour, each surface's pinned markup
// ---------------------------------------------------------------------------
describe('preview strip', () => {
  it('renders nothing when nothing is previewed', () => {
    const { container } = render(<VersionPreviewStrip variant="drawer" version={null} onBackToHead={() => {}} />)
    expect(container.innerHTML).toBe('')
  })

  it('keeps /try’s pinned copy and its write-lock note', () => {
    render(<VersionPreviewStrip variant="tab" version={1} onBackToHead={() => {}} />)
    expect(screen.getByText(/Viewing v1 read-only/)).toBeTruthy()
    expect(screen.getByTestId('try-preview-write-lock').textContent)
      .toBe('Editing is paused until you return to head.')
  })

  it('keeps /app’s pinned copy', () => {
    render(<VersionPreviewStrip variant="drawer" version={2} latest={3} onBackToHead={() => {}} />)
    expect(screen.getByText('Viewing v2 of 3 — read-only preview')).toBeTruthy()
  })

  it('back-to-head fires on both skins', () => {
    for (const variant of ['drawer', 'tab']) {
      const onBackToHead = vi.fn()
      const { unmount } = render(<VersionPreviewStrip variant={variant} version={1} onBackToHead={onBackToHead} />)
      fireEvent.click(screen.getByText('Back to head'))
      expect(onBackToHead).toHaveBeenCalledTimes(1)
      unmount()
    }
  })
})
