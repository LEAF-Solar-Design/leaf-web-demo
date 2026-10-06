// @vitest-environment jsdom
/**
 * S23 agent checkpoints: the Rewind action in version history.
 *
 * Claims under test, each failing before the slice (no Rewind existed):
 *  1. Rewind appears only on a version carrying the agent-turn marker
 *     (`turn_id`), and only on the head, because it IS the Undo step.
 *  2. It sits OUTSIDE the .vh-row button (no button inside a button), beside
 *     where .vh-restore sits inside .vh-row-line.
 *  3. It carries the coverage sentence: what it does not undo.
 *  4. It only calls onUndo: no restore request, no preview, no restore commit.
 *
 * Run:  cd web && npx vitest run src/components/versionRewind.test.jsx
 */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api.js', async (importOriginal) => ({
  ...(await importOriginal()),
  getDrawingVersions: vi.fn(async () => { throw new Error('not expected') }),
  restoreDrawingVersion: vi.fn(async () => { throw new Error('not expected') }),
}))

import { getDrawingVersions, restoreDrawingVersion } from '../api.js'
import VersionHistory from './VersionHistory.jsx'
import VersionList, { isAgentTurnVersion } from './VersionList.jsx'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// The sentence the control must carry, written out here rather than imported,
// so a change to the copy is a deliberate change to this oracle too.
const COVERAGE =
  'Rewind undoes this agent turn\'s drawing change only; server side effects and published tools stay as they are.'

const TURN = 'turn-7f3a'

function chain({ headTurn = TURN, midTurn = null } = {}) {
  return [
    { v: 1, parent: null, created: '2020-03-01T12:00:00Z', sha256: 'aaaaaaaaaaaaaaaa', tool: null, note: null, delta: null, source_ref: null },
    { v: 2, parent: 1, created: '2020-03-02T12:00:00Z', sha256: 'bbbbbbbbbbbbbbbb', tool: 'drawing.write', note: 'second', delta: null, source_ref: null, ...(midTurn ? { turn_id: midTurn } : {}) },
    { v: 3, parent: 2, created: '2020-03-03T12:00:00Z', sha256: 'cccccccccccccccc', tool: 'drawing.write', note: 'third', delta: null, source_ref: null, ...(headTurn != null ? { turn_id: headTurn } : {}) },
  ]
}

function renderDrawer({ versions = chain(), ...props } = {}) {
  const handlers = {
    onUndo: vi.fn(async () => {}),
    onPreview: vi.fn(),
    onRestored: vi.fn(),
    onBeforeRestore: vi.fn(),
    onBackToHead: vi.fn(),
  }
  const view = render(
    <VersionHistory
      data={{ drawing_id: 'demo', head: 3, latest: 3, versions }}
      error={null}
      loading={false}
      previewingVersion={null}
      onClose={() => {}}
      onRetry={() => {}}
      retryKey={false}
      exiting={false}
      mock
      capability={null}
      headWarning={null}
      mutationBlocked={false}
      {...handlers}
      {...props}
    />,
  )
  return { ...view, handlers }
}

const rewinds = (container) => [...container.querySelectorAll('button')].filter((b) => /^Rewind/.test(b.textContent))

describe('the agent-turn marker', () => {
  it('is a bounded non-empty turn_id string and nothing else', () => {
    expect(isAgentTurnVersion({ v: 3, turn_id: TURN })).toBe(true)
    for (const bad of [undefined, null, '', 7, {}, [], true, 'x'.repeat(129)]) {
      expect(isAgentTurnVersion({ v: 3, turn_id: bad })).toBe(false)
    }
    expect(isAgentTurnVersion(null)).toBe(false)
    expect(isAgentTurnVersion({ v: 3, workitem_id: 'wi', tool: 'drawing.write' })).toBe(false)
  })
})

describe('Rewind appears only on agent-made versions', () => {
  it('shows exactly one Rewind, on the agent-made head row', () => {
    const { container, getByTestId } = renderDrawer()
    const found = rewinds(container)
    expect(found).toHaveLength(1)
    expect(getByTestId('vh-row-v3').contains(found[0])).toBe(true)
    expect(found[0].getAttribute('data-testid')).toBe('version-rewind-v3')
  })

  it('shows no Rewind when no version carries the marker', () => {
    const { container } = renderDrawer({ versions: chain({ headTurn: null }) })
    expect(rewinds(container)).toHaveLength(0)
    expect(container.querySelector('.vh-rewind')).toBeNull()
  })

  it('shows no Rewind on an agent-made row that is not the head (Undo would undo the head)', () => {
    const { container } = renderDrawer({ versions: chain({ headTurn: null, midTurn: TURN }) })
    expect(rewinds(container)).toHaveLength(0)
  })

  it('shows no Rewind for a malformed marker', () => {
    for (const bad of ['', 42, 'x'.repeat(129)]) {
      const { container, unmount } = renderDrawer({ versions: chain({ headTurn: bad }) })
      expect(rewinds(container)).toHaveLength(0)
      unmount()
    }
  })

  it('shows no Rewind when the shell wires no Undo', () => {
    const { container } = renderDrawer({ onUndo: undefined })
    expect(rewinds(container)).toHaveLength(0)
  })
})

describe('Rewind placement', () => {
  it('sits outside the .vh-row button, inside .vh-row-line beside the row button', () => {
    const { container, getByTestId } = renderDrawer()
    const [rewind] = rewinds(container)
    expect(rewind.closest('.vh-row')).toBeNull()
    const line = getByTestId('vh-row-v3').querySelector('.vh-row-line')
    expect(rewind.closest('.vh-row-line')).toBe(line)
    const holder = rewind.closest('.vh-rewind')
    expect(holder).not.toBeNull()
    expect(holder.parentElement).toBe(line)
    expect([...line.children].some((el) => el.matches('button.vh-row'))).toBe(true)
    expect(container.querySelectorAll('button button')).toHaveLength(0)
  })

  it('sits beside .vh-restore on a row that has both', () => {
    // Head v3 is agent-made; v2 carries a Restore. The Rewind holder follows
    // the restore slot's position: a direct child of the row line.
    const { container } = renderDrawer()
    for (const slot of container.querySelectorAll('.vh-restore, .vh-rewind')) {
      expect(slot.parentElement.classList.contains('vh-row-line')).toBe(true)
    }
  })
})

describe('Rewind says what it does not cover', () => {
  it('carries the coverage sentence', () => {
    const { container } = renderDrawer()
    const [rewind] = rewinds(container)
    expect(rewind.getAttribute('title')).toBe(COVERAGE)
    expect(COVERAGE).toMatch(/server side effects/)
    expect(COVERAGE).toMatch(/published tools/)
  })
})

describe('Rewind only calls onUndo', () => {
  it('runs onUndo once, with no arguments, and nothing else', async () => {
    const { container, handlers } = renderDrawer()
    const [rewind] = rewinds(container)
    await act(async () => { fireEvent.click(rewind) })
    expect(handlers.onUndo).toHaveBeenCalledTimes(1)
    expect(handlers.onUndo.mock.calls[0]).toEqual([])
    expect(handlers.onPreview).not.toHaveBeenCalled()
    expect(handlers.onRestored).not.toHaveBeenCalled()
    expect(handlers.onBeforeRestore).not.toHaveBeenCalled()
    expect(handlers.onBackToHead).not.toHaveBeenCalled()
    expect(restoreDrawingVersion).not.toHaveBeenCalled()
    expect(getDrawingVersions).not.toHaveBeenCalled()
  })

  it('is disabled under the same blocks as the ribbon Undo, and a disabled click calls nothing', async () => {
    for (const props of [{ undoDisabled: true }, { mutationBlocked: true }, { previewingVersion: 2 }]) {
      const { container, handlers, unmount } = renderDrawer(props)
      const [rewind] = rewinds(container)
      expect(rewind.disabled).toBe(true)
      await act(async () => { fireEvent.click(rewind) })
      expect(handlers.onUndo).not.toHaveBeenCalled()
      unmount()
    }
  })

  it('is single-flight: a second click while the first Undo runs calls nothing more', async () => {
    let finish
    const onUndo = vi.fn(() => new Promise((resolve) => { finish = resolve }))
    const { container } = renderDrawer({ onUndo })
    const [rewind] = rewinds(container)
    await act(async () => { fireEvent.click(rewind) })
    expect(rewind.disabled).toBe(true)
    await act(async () => { fireEvent.click(rewind) })
    expect(onUndo).toHaveBeenCalledTimes(1)
    await act(async () => { finish() })
    expect(rewind.disabled).toBe(false)
  })
})

describe('/try tab skin', () => {
  it('renders Rewind beside the row button, not inside it, and only on the agent-made head', async () => {
    const run = vi.fn(async () => {})
    const { container, getByTestId } = render(
      <VersionList variant="tab" versions={chain({ midTurn: TURN })} head={3} onPreview={() => {}} rewind={{ run, disabled: false }} />,
    )
    const found = rewinds(container)
    expect(found).toHaveLength(1)
    expect(getByTestId('try-version-v3').contains(found[0])).toBe(true)
    expect(found[0].parentElement).toBe(getByTestId('try-version-v3'))
    expect(found[0].getAttribute('title')).toBe(COVERAGE)
    expect(container.querySelectorAll('button button')).toHaveLength(0)
    await act(async () => { fireEvent.click(found[0]) })
    expect(run).toHaveBeenCalledTimes(1)
    expect(run.mock.calls[0]).toEqual([])
  })
})
