// @vitest-environment jsdom
import React from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import golden from '../leafPlatform/__fixtures__/leaf.send-copy.v1.golden.json'
import { fingerprintLine } from '../leafPlatform/sendCopy.js'
import { DrawingIdentityProvider } from './DrawingIdentityProvider.jsx'
import SentCopyLine, { useLandOnQueryDrawing } from './SentCopyHeader.jsx'
import { queryDrawingVersionId, sentCopyHeaderLine } from './sentCopyHeader.js'

const DRAWING = '0c0c0c0c-0000-4000-8000-000000000003'
const OTHER = '0d0d0d0d-0000-4000-8000-000000000004'
const PROJECT = '0b0b0b0b-0000-4000-8000-000000000002'
const payload = JSON.parse(golden.cases.find((entry) => entry.name === 'sent').wire).payload
const LINE = 'Parking.dwg | sent copy 8e0fcf8e | BUILDING 1, SITE 1 | 2 objects in model space'
const workspace = {
  project: { project_id: PROJECT },
  drawing_artifacts: [{ drawing_id: DRAWING, name: 'Parking.dwg' }],
  drawing_versions: [
    { version_id: 'v-other', drawing_id: OTHER, seq: 9 },
    { version_id: 'v1', drawing_id: DRAWING, seq: 1 },
    { version_id: 'v2', drawing_id: DRAWING, seq: 2 },
    { version_id: 'v3', drawing_id: DRAWING, seq: 3, deleted_at: '2026-01-01T00:00:00Z' },
  ],
}
const ready = (overrides = {}) => ({
  status: 'ready', tenant_kind: 'account', filename: 'Parking.dxf',
  content_sha256: payload.sha256, fingerprint: payload.fingerprint, ...overrides,
})

function Identity({ search = `?drawing=${DRAWING}`, children }) {
  return <DrawingIdentityProvider mode="console" scene="app" search={search} publicDemo={false} liveDemo={false}>
    {children}
  </DrawingIdentityProvider>
}

function LandingProbe(props) {
  useLandOnQueryDrawing(props)
  return null
}

function mountLanding(overrides = {}, search = `?drawing=${DRAWING}`) {
  const props = { mock: false, openProjectId: PROJECT, workspace, canonicalVersionId: null,
    selectCanonicalVersion: vi.fn(), ...overrides }
  const tree = (next) => <Identity search={search}><LandingProbe {...props} {...next} /></Identity>
  const view = render(tree())
  return { ...view, select: props.selectCanonicalVersion, update: (next) => view.rerender(tree(next)) }
}

afterEach(cleanup)

describe('SentCopyLine', () => {
  it('renders the artifact name and the exact palette fingerprint line', async () => {
    const load = vi.fn(async () => ready())
    render(<Identity><SentCopyLine mock={false} shownDrawingId={DRAWING} workspace={workspace} load={load} /></Identity>)
    const line = await screen.findByTestId('sent-copy-line')
    expect(line.textContent).toBe(LINE)
    expect(line.textContent).toBe(fingerprintLine(payload.fingerprint, payload.fileName))
    expect(line.title).toBe(LINE)
    expect(line.className).toBe('sent-copy-line')
    expect(load).toHaveBeenCalledExactlyOnceWith(DRAWING)
  })

  it.each([
    ['a different shown drawing', { shownDrawingId: OTHER }, `?drawing=${DRAWING}`],
    ['mock mode', { mock: true }, `?drawing=${DRAWING}`],
    ['no drawing query', {}, ''],
  ])('does not load or render for %s', async (_label, overrides, search) => {
    const load = vi.fn(async () => ready())
    await act(async () => {
      render(<Identity search={search}>
        <SentCopyLine mock={false} shownDrawingId={DRAWING} workspace={workspace} load={load} {...overrides} />
      </Identity>)
    })
    expect(screen.queryByTestId('sent-copy-line')).toBeNull()
    expect(load).not.toHaveBeenCalled()
  })

  it.each([
    ['a fingerprint that is not ok', ready({ fingerprint: { ...payload.fingerprint, status: 'unavailable' } })],
    ['different content bytes', ready({ content_sha256: 'different' })],
  ])('renders nothing for %s', async (_label, status) => {
    const load = vi.fn(async () => status)
    await act(async () => {
      render(<Identity><SentCopyLine shownDrawingId={DRAWING} workspace={workspace} load={load} /></Identity>)
    })
    expect(load).toHaveBeenCalledExactlyOnceWith(DRAWING)
    expect(screen.queryByTestId('sent-copy-line')).toBeNull()
  })

  it('renders nothing for a rejected upload status and never retries on rerender', async () => {
    const load = vi.fn(async () => { throw Object.assign(new Error('not an upload'), { status: 404 }) })
    const tree = (nextWorkspace) => <Identity>
      <SentCopyLine shownDrawingId={DRAWING} workspace={nextWorkspace} load={load} />
    </Identity>
    let view
    await act(async () => { view = render(tree(workspace)) })
    await act(async () => { view.rerender(tree({ ...workspace })) })
    expect(load).toHaveBeenCalledTimes(1)
    expect(screen.queryByTestId('sent-copy-line')).toBeNull()
  })

  it('compares drawing ids without case and loads once through effect replay and shown-version changes', async () => {
    const load = vi.fn(async () => ready())
    const tree = (shownDrawingId, nextWorkspace = workspace) => <React.StrictMode><Identity>
      <SentCopyLine shownDrawingId={shownDrawingId} workspace={nextWorkspace} load={load} />
    </Identity></React.StrictMode>
    const view = render(tree(DRAWING.toUpperCase()))
    expect((await screen.findByTestId('sent-copy-line')).textContent).toBe(LINE)
    view.rerender(tree(OTHER))
    expect(screen.queryByTestId('sent-copy-line')).toBeNull()
    await act(async () => { view.rerender(tree(DRAWING)) })
    expect(screen.getByTestId('sent-copy-line').textContent).toBe(LINE)
    view.rerender(tree(DRAWING, { ...workspace, drawing_artifacts: [{ drawing_id: DRAWING, name: 'Renamed.dwg' }] }))
    expect(screen.getByTestId('sent-copy-line').textContent).toBe(fingerprintLine(payload.fingerprint, 'Renamed.dwg'))
    expect(load).toHaveBeenCalledExactlyOnceWith(DRAWING)
  })

  it('ignores a pending result after unmount when another query drawing is mounted', async () => {
    let resolveOld
    const oldLoad = vi.fn(() => new Promise((resolve) => { resolveOld = resolve }))
    const view = render(<Identity><SentCopyLine shownDrawingId={DRAWING} workspace={workspace} load={oldLoad} /></Identity>)
    await act(async () => {})
    view.unmount()
    const nextLoad = vi.fn(async () => ready({ filename: 'Other.dxf' }))
    render(<Identity search={`?drawing=${OTHER}`}><SentCopyLine shownDrawingId={OTHER} workspace={workspace} load={nextLoad} /></Identity>)
    const line = await screen.findByTestId('sent-copy-line')
    expect(line.textContent).toBe(fingerprintLine(payload.fingerprint, 'Other.dxf'))
    await act(async () => { resolveOld(ready()) })
    expect(line.textContent).toBe(fingerprintLine(payload.fingerprint, 'Other.dxf'))
    expect(nextLoad).toHaveBeenCalledExactlyOnceWith(OTHER)
  })

  it('renders names as text, including markup-looking names', async () => {
    const name = '<img src=x onerror=alert(1)>.dwg'
    render(<Identity><SentCopyLine shownDrawingId={DRAWING}
      workspace={{ drawing_artifacts: [{ drawing_id: DRAWING, name }] }} load={async () => ready()} /></Identity>)
    const line = await screen.findByTestId('sent-copy-line')
    expect(line.textContent).toBe(fingerprintLine(payload.fingerprint, name))
    expect(line.children).toHaveLength(0)
  })
})

describe('useLandOnQueryDrawing', () => {
  it('selects only the newest live version of the query drawing once, preserving later user choices and clearing', () => {
    const view = mountLanding()
    expect(view.select).toHaveBeenCalledExactlyOnceWith('v2')
    view.update({ canonicalVersionId: 'v-other' })
    view.update({ canonicalVersionId: null, workspace: { ...workspace } })
    expect(view.select).toHaveBeenCalledExactlyOnceWith('v2')
  })

  it.each([
    ['an existing canonical selection', { canonicalVersionId: 'v-other' }, `?drawing=${DRAWING}`],
    ['mock mode', { mock: true }, `?drawing=${DRAWING}`],
    ['no drawing query', {}, ''],
    ['a different project', { openProjectId: 'another-project' }, `?drawing=${DRAWING}`],
    ['an absent drawing', {}, '?drawing=absent'],
  ])('does not select for %s', (_label, overrides, search) => {
    const view = mountLanding(overrides, search)
    expect(view.select).not.toHaveBeenCalled()
  })

  it('waits for workspace hydration before remembering the landing', () => {
    const view = mountLanding({ workspace: null })
    expect(view.select).not.toHaveBeenCalled()
    view.update({ workspace })
    expect(view.select).toHaveBeenCalledExactlyOnceWith('v2')
  })

  it('remembers each project and source pair when switching projects and returning', () => {
    const view = mountLanding()
    view.update({ openProjectId: 'another-project', workspace: { ...workspace, project: { id: 'another-project' } } })
    expect(view.select.mock.calls).toEqual([['v2'], ['v2']])
    view.update({ openProjectId: PROJECT, workspace })
    expect(view.select).toHaveBeenCalledTimes(2)
  })

  it('selects once under StrictMode effect replay', () => {
    const selectCanonicalVersion = vi.fn()
    render(<React.StrictMode><Identity><LandingProbe mock={false} openProjectId={PROJECT}
      workspace={workspace} canonicalVersionId={null} selectCanonicalVersion={selectCanonicalVersion} /></Identity></React.StrictMode>)
    expect(selectCanonicalVersion).toHaveBeenCalledExactlyOnceWith('v2')
  })
})

describe('sentCopyHeaderLine', () => {
  const line = (status = ready(), nextWorkspace = workspace, drawingId = DRAWING) =>
    sentCopyHeaderLine({ status, workspace: nextWorkspace, drawingId })

  it('prefers the matching artifact name with a case-insensitive id match', () => {
    expect(line(ready(), { drawing_artifacts: [
      { drawing_id: OTHER, name: 'Wrong.dwg' },
      { drawing_id: DRAWING.toUpperCase(), name: 'Parking.dwg' },
    ] })).toBe(LINE)
  })

  it('uses the upload filename when no matching artifact name is available', () => {
    const expected = fingerprintLine(payload.fingerprint, 'Parking.dxf')
    expect(line(ready(), null)).toBe(expected)
    expect(line(ready(), { drawing_artifacts: [{ drawing_id: OTHER, name: 'Wrong.dwg' }] })).toBe(expected)
    expect(line(ready(), { drawing_artifacts: [{ drawing_id: DRAWING, name: '' }] })).toBe(expected)
  })

  it.each([
    ['missing status', null],
    ['not ready', ready({ status: 'processing' })],
    ['guest tenant', ready({ tenant_kind: 'guest' })],
    ['missing tenant', ready({ tenant_kind: undefined })],
    ['missing fingerprint', ready({ fingerprint: null })],
    ['non-object fingerprint', ready({ fingerprint: 'ok' })],
    ['unknown schema', ready({ fingerprint: { ...payload.fingerprint, schema: 'unknown' } })],
    ['fingerprint not ok', ready({ fingerprint: { ...payload.fingerprint, status: 'unavailable' } })],
    ['missing content hash', ready({ content_sha256: undefined })],
    ['non-string hashes', ready({ content_sha256: 123, fingerprint: { ...payload.fingerprint, sha256: 123 } })],
    ['different hash', ready({ content_sha256: 'different' })],
  ])('returns null for %s', (_label, status) => {
    expect(line(status)).toBeNull()
  })

  it.each([undefined, null, '', '   ', 123])('returns null when neither filename is usable (%s)', (filename) => {
    expect(line(ready({ filename }), { drawing_artifacts: [{ drawing_id: DRAWING, name: '' }] })).toBeNull()
  })
})

describe('queryDrawingVersionId', () => {
  const version = (overrides = {}) => queryDrawingVersionId({ workspace, openProjectId: PROJECT, drawingId: DRAWING, ...overrides })

  it('chooses the highest numeric seq, ignoring other drawings and deleted versions', () => {
    expect(version()).toBe('v2')
    expect(version({ drawingId: DRAWING.toUpperCase() })).toBe('v2')
    expect(version({ workspace: { ...workspace, project: { id: PROJECT }, drawing_versions: [
      { version_id: 'v10', drawing_id: DRAWING.toUpperCase(), seq: '10', deleted_at: null },
      ...workspace.drawing_versions,
      { version_id: 11, drawing_id: DRAWING, seq: 11 },
      { version_id: 'invalid', drawing_id: DRAWING, seq: 'bad' },
    ] } })).toBe('v10')
  })

  it.each([
    ['missing workspace', { workspace: null }],
    ['missing project', { workspace: { drawing_versions: workspace.drawing_versions }, openProjectId: undefined }],
    ['different project', { openProjectId: 'another-project' }],
    ['absent drawing', { drawingId: 'absent' }],
    ['empty drawing', { drawingId: '' }],
    ['whitespace drawing', { drawingId: '   ' }],
    ['non-string drawing', { drawingId: 123 }],
    ['missing drawing', { drawingId: undefined }],
    ['missing versions', { workspace: { project: { project_id: PROJECT } } }],
    ['only deleted versions', { workspace: { project: { id: PROJECT }, drawing_versions: [workspace.drawing_versions[3]] } }],
  ])('returns null for %s', (_label, overrides) => {
    expect(version(overrides)).toBeNull()
  })
})
