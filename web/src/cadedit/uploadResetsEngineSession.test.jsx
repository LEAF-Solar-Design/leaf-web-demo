/**
 * Upload -> engine-session reset, END TO END across the two W1 owners
 * (panel W1 finding 5).
 *
 * Both halves of this were already proven, separately: drawingIdentity's
 * specs prove an upload receipt promotes the identity, and engineSession's
 * specs prove a `drawingId` PROP change resets the session and tears the
 * worker down. Nothing drove the WIRE between them, so the two could agree
 * about their own contracts while disagreeing about each other's — an upload
 * that promoted the identity without moving the value the store watches would
 * have left engine state from the previous document open under a new drawing,
 * and every existing test would still be green.
 *
 * So this drives the REAL provider and the REAL store together: an upload
 * promotion goes in at the provider, and the store's own reset comes out.
 * The worker is a scripted TRANSPORT double (the same device engineSession's
 * specs use) — the test decides what it answers, and everything asserted is
 * the shipped state machine.
 */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import {
  DRAWING_MODE_OPERATOR,
  DrawingIdentityProvider,
  useDrawingIdentity,
} from '../drawing/DrawingIdentityProvider.jsx'

import useEngineSession, { GEOMETRY_SOURCE } from './engineSession.js'

afterEach(cleanup)

class ScriptedWorker {
  constructor() {
    this.posted = []
    this.listeners = { message: [], error: [], messageerror: [] }
    this.terminated = false
  }

  addEventListener(type, cb) {
    if (this.listeners[type]) this.listeners[type].push(cb)
  }

  removeEventListener() {}

  postMessage(data) { this.posted.push(data) }

  terminate() { this.terminated = true }

  emit(data) {
    act(() => { this.listeners.message.forEach((cb) => cb({ data })) })
  }
}

const LINE = { id: 'e1', type: 'LINE', layer: 'Panels', vertices: [[0, 0], [1, 1]] }

function loadedMessage(entities, documentId) {
  return { type: 'documentLoaded', documentId, entities, entityCount: entities.length, unsupported: [] }
}

function fileOf(name) {
  const bytes = new TextEncoder().encode('0\nEOF\n')
  const file = new File([bytes], name, { type: 'application/dxf' })
  file.arrayBuffer = async () => bytes.buffer.slice(0)
  Object.defineProperty(file, 'size', { value: bytes.length })
  return file
}

// The real pair, wired exactly as the cockpit wires them: the surface reads
// the provider and hands the store that drawing id (CadEditSurface.jsx).
function mountStudio() {
  const workers = []
  const createWorker = () => {
    const worker = new ScriptedWorker()
    workers.push(worker)
    return worker
  }
  const handle = { workers }

  function Host() {
    const identity = useDrawingIdentity()
    handle.identity = identity
    handle.session = useEngineSession({ createWorker, drawingId: identity.drawingId })
    return null
  }

  render(
    <DrawingIdentityProvider
      mode={DRAWING_MODE_OPERATOR}
      search=""
      publicDemo={false}
      liveDemo={false}
      readLiveDrawingId={() => null}
      rememberDrawingId={() => true}
      readAuthToken={() => null}
      subscribeAuthChange={() => () => {}}
    >
      <Host />
    </DrawingIdentityProvider>,
  )
  return handle
}

async function openAndLoad(studio, name) {
  await act(async () => { await studio.session.actions.open(fileOf(name)) })
  studio.workers[studio.workers.length - 1].emit(loadedMessage([LINE], name))
}

describe('an upload promotion resets the engine session (provider + store, both real)', () => {
  it('tears the open document down when the upload moves the drawing identity', async () => {
    const studio = mountStudio()
    expect(studio.identity.drawingId).toBeNull()

    await openAndLoad(studio, 'before-upload.dxf')
    expect(studio.session.entityCount).toBe(1)
    expect(studio.session.engineParsed).toBe(true)
    expect(studio.session.geometrySource).toBe(GEOMETRY_SOURCE.ENGINE_PARSE)
    expect(studio.session.documentId).toBe('before-upload.dxf')

    // THE WIRE: the promotion goes in at the provider only.
    act(() => { studio.identity.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })

    expect(studio.identity.drawingId).toBe('u-guest-1')
    expect(studio.identity.origin).toBe('upload')
    // ...and the store's reset comes out, with no engine state carried over.
    expect(studio.session.documentId).toBe('')
    expect(studio.session.entities).toEqual([])
    expect(studio.session.entityCount).toBe(0)
    expect(studio.session.engineParsed).toBe(false)
    expect(studio.session.geometrySource).toBeNull()
    expect(studio.session.selectedId).toBe('')
    expect(studio.workers[0].terminated).toBe(true)
  })

  it('abandons a reply from the pre-upload document rather than seating it under the new drawing', async () => {
    const studio = mountStudio()
    await openAndLoad(studio, 'before-upload.dxf')
    const stale = studio.workers[0]

    act(() => { studio.identity.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })

    // A reply that raced the switch. The generation guard must drop it: this
    // is the cross-document bleed the ACCEPTANCE state names.
    stale.emit(loadedMessage([LINE, { ...LINE, id: 'e2' }], 'before-upload.dxf'))
    expect(studio.session.entityCount).toBe(0)
    expect(studio.session.engineParsed).toBe(false)
  })

  it('the next open after the promotion spawns a FRESH worker for the new drawing', async () => {
    const studio = mountStudio()
    await openAndLoad(studio, 'before-upload.dxf')
    act(() => { studio.identity.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })

    await openAndLoad(studio, 'after-upload.dxf')
    expect(studio.workers).toHaveLength(2)
    expect(studio.workers[1].terminated).toBe(false)
    expect(studio.session.documentId).toBe('after-upload.dxf')
    expect(studio.session.entityCount).toBe(1)
  })

  it('a receipt that promotes NOTHING leaves the open document alone', async () => {
    const studio = mountStudio()
    act(() => { studio.identity.setFromUpload({ drawing_id: 'u-guest-1', tenant_kind: 'guest' }) })
    await openAndLoad(studio, 'held.dxf')
    expect(studio.session.entityCount).toBe(1)

    // No drawing id in the receipt: the identity does not move, so the store
    // must not reset a document the operator is still editing.
    act(() => { studio.identity.setFromUpload({ drawing_id: '', tenant_kind: 'guest' }) })
    expect(studio.identity.drawingId).toBe('u-guest-1')
    expect(studio.session.documentId).toBe('held.dxf')
    expect(studio.session.entityCount).toBe(1)
    expect(studio.workers[0].terminated).toBe(false)
  })
})
