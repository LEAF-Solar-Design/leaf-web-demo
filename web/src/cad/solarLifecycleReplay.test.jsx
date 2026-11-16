// @vitest-environment jsdom
// Studio18 Solar lifecycle regression: real React session and isolated worker.
import { scaleTimeout } from '../testTimeScale.js'
import { execFileSync } from 'node:child_process'
import { existsSync, mkdtempSync, readdirSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { afterEach, describe, expect, it } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import useEngineSession from '../cadedit/engineSession.js'

function createEditorWorker() {
  return new Worker(
    new URL('../../../vendor/acadrust-worker/worker-browser.mjs', import.meta.url),
    { type: 'module' },
  )
}
function captureWorkerPath() {
  class CapturingWorker {
    constructor(url) {
      const u = url instanceof URL ? url : new URL(String(url))
      CapturingWorker.captured = u.protocol === 'file:'
        ? fileURLToPath(u)
        : decodeURIComponent(u.pathname.replace(/^\/@fs\//, '/').replace(/^\/(?=[A-Za-z]:)/, ''))
    }
  }
  const previous = globalThis.Worker
  globalThis.Worker = CapturingWorker
  try {
    createEditorWorker()
  } finally {
    if (previous === undefined) delete globalThis.Worker
    else globalThis.Worker = previous
  }
  return CapturingWorker.captured
}
const WORKER_PATH = captureWorkerPath()
const PKG_DIR = path.join(path.dirname(WORKER_PATH), 'pkg-node')
const PKG_NAMES = existsSync(PKG_DIR) ? readdirSync(PKG_DIR) : []
const GLUE = PKG_NAMES.includes('engine.js') ? 'engine.js' : PKG_NAMES.find((name) => name.endsWith('_worker.js'))
afterEach(cleanup)

function realWorkerTransport() {
  const listeners = new Set()
  const worker = { posted: [], writes: [], bytes: null, documentId: null,
    addEventListener(type, listener) { if (type === 'message') listeners.add(listener) },
    removeEventListener(type, listener) { if (type === 'message') listeners.delete(listener) },
    terminate() { listeners.clear() },
    postMessage(message) {
      worker.posted.push(message)
      if (message.type === 'dispose') return
      queueMicrotask(() => {
        const source = [
          'import { createRequire } from "node:module"',
          'import { pathToFileURL } from "node:url"',
          'import { readFileSync } from "node:fs"',
          'const [workerPath, gluePath, inputPath] = process.argv.slice(1)',
          'const { handleMessage } = await import(pathToFileURL(workerPath).href)',
          'const native = createRequire(import.meta.url)(gluePath)',
          'let writes = 0',
          'const engine = { ...native, writeDxf(doc) { writes++; return native.writeDxf(doc) } }',
          'const { message, previous, documentId } = JSON.parse(readFileSync(inputPath, "utf8"))',
          'if (previous && message.type === "applyEdit") await handleMessage({ type: "loadDocument", documentId, bytes: Uint8Array.from(previous) }, engine)',
          'if (message.bytes) message.bytes = Uint8Array.from(message.bytes)',
          'writes = 0',
          'const reply = await handleMessage(message, engine)',
          'if (reply?.bytes) reply.bytes = Array.from(reply.bytes)',
          'process.stdout.write(JSON.stringify({ reply, writes }))',
        ].join('\n')
        const input = JSON.stringify({ message: { ...message, ...(message.bytes ? { bytes: Array.from(message.bytes) } : {}) }, previous: worker.bytes, documentId: worker.documentId })
        const inputDir = mkdtempSync(path.join(tmpdir(), 'engine-batch-'))
        const inputPath = path.join(inputDir, 'input.json')
        let response
        try {
          writeFileSync(inputPath, input, 'utf8')
          response = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', source, WORKER_PATH, path.join(PKG_DIR, GLUE), inputPath], { encoding: 'utf8', timeout: scaleTimeout(90_000), maxBuffer: 16 * 1024 * 1024 }))
        } finally {
          rmSync(inputDir, { recursive: true, force: true })
        }
        const { reply, writes } = response
        worker.writes.push({ type: message.type, op: message.op, count: writes })
        if (message.type === 'loadDocument') { worker.bytes = Array.from(message.bytes); worker.documentId = message.documentId }
        if (reply?.bytes) { worker.bytes = reply.bytes; reply.bytes = Uint8Array.from(reply.bytes) }
        if (reply) listeners.forEach((listener) => listener({ data: reply }))
      })
    },
  }
  return worker
}

// Two synthetic frame outlines. No benchmark or customer material.
function fixture(dx = 0) {
  const pairs = [[0, 'SECTION'], [2, 'HEADER'], [9, '$INSUNITS'], [70, 6], [0, 'ENDSEC'], [0, 'SECTION'], [2, 'ENTITIES']]
  for (let i = 0; i < 2; i++) {
    pairs.push([0, 'LWPOLYLINE'], [5, (16 + i).toString(16)], [100, 'AcDbEntity'], [8, 'LEAF-TRACKERS'], [100, 'AcDbPolyline'], [90, 4], [70, 1])
    for (const [x, y] of [[0, 0], [2, 0], [2, 6], [0, 6]]) pairs.push([10, x + dx + i * 4], [20, y])
  }
  pairs.push([0, 'ENDSEC'], [0, 'EOF'])
  return new TextEncoder().encode(pairs.flat().join('\n') + '\n')
}
const original = fixture()
const clone = value => JSON.parse(JSON.stringify(value))
const sameBytes = (a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)) === 0

describe.skipIf(!GLUE)('Solar document lifecycle on the real engine', () => {

it('divergent edit invalidates redo and restores original bytes',async()=>{
 const worker=realWorkerTransport(),{result}=renderHook(()=>useEngineSession({createWorker:()=>worker}))
 await act(async()=>{result.current.actions.openBytes(original,'history.dxf',{committed:true})})
 const before=clone(Array.from(result.current.entities)),chosen=before.find(r=>r.type==='LWPOLYLINE'&&r.editable!==false)
 act(()=>result.current.actions.select(chosen.id))
 const move=async(dx)=>{await act(async()=>{result.current.actions.applyEdit('move',{dx:String(dx),dy:'0'})});expect(result.current.errorKind).toBeNull()}
 await move(1);const first=clone(Array.from(result.current.entities));await move(2)
 expect(result.current.undoDepth).toBe(2)
 await act(async()=>{expect(result.current.actions.undo()).toBe(true)})
 expect(Array.from(result.current.entities)).toEqual(first);expect(result.current.redoDepth).toBe(1)
 await move(-4);const divergent=clone(Array.from(result.current.entities))
 expect(result.current.redoDepth).toBe(0)
 act(()=>{expect(result.current.actions.redo()).toBe(false)})
 expect(Array.from(result.current.entities)).toEqual(divergent)
 const expected=chosen.vertices.map(([x,y,...rest])=>[x-3,y,...rest])
 expect(divergent.find(r=>r.id===chosen.id).vertices).toEqual(expected)
 expect(divergent.filter(r=>r.id!==chosen.id)).toEqual(before.filter(r=>r.id!==chosen.id))
 await act(async()=>{expect(result.current.actions.undo()).toBe(true)})
 expect(Array.from(result.current.entities)).toEqual(first)
 await act(async()=>{expect(result.current.actions.undo()).toBe(true)})
 expect(Array.from(result.current.entities)).toEqual(before)
 expect(sameBytes(worker.posted.at(-1).bytes,original)).toBe(true)
},scaleTimeout(90_000))

it('delayed old load cannot replace new document or history',async()=>{
 const inner=realWorkerTransport(),listeners=new Set(),queued=[]
 inner.addEventListener('message',e=>queued.push(e))
 const worker={postMessage:m=>inner.postMessage(m),terminate:()=>inner.terminate(),addEventListener:(t,f)=>{if(t==='message')listeners.add(f)},removeEventListener:(t,f)=>listeners.delete(f)}
 const deliver=e=>listeners.forEach(f=>f(e))
 const {result}=renderHook(()=>useEngineSession({createWorker:()=>worker}))
 await act(async()=>{result.current.actions.openBytes(original,'old.dxf',{committed:true})})
 const old=queued.splice(0);expect(old.some(e=>e.data.type==='documentLoaded')).toBe(true)
 const second=fixture(1000)
 await act(async()=>{result.current.actions.openBytes(second,'new.dxf',{committed:true})})
 const current=queued.splice(0);expect(current.some(e=>e.data.type==='documentLoaded')).toBe(true)
 act(()=>current.forEach(deliver))
 expect(result.current.documentId).toBe('new.dxf');const before=clone(Array.from(result.current.entities));expect(before.length).toBe(2)
 act(()=>old.forEach(deliver))
 expect(result.current.documentId).toBe('new.dxf');expect(Array.from(result.current.entities)).toEqual(before)
 expect(result.current.undoDepth).toBe(0);expect(result.current.redoDepth).toBe(0)
 const chosen=before.find(r=>r.type==='LWPOLYLINE'&&r.editable!==false);act(()=>result.current.actions.select(chosen.id))
 await act(async()=>{result.current.actions.applyEdit('move',{dx:'1',dy:'0'})})
 const edit=queued.splice(0);act(()=>edit.forEach(deliver))
 expect(result.current.undoDepth).toBe(1);const moved=clone(Array.from(result.current.entities))
 act(()=>old.forEach(deliver));expect(Array.from(result.current.entities)).toEqual(moved);expect(result.current.undoDepth).toBe(1)
 await act(async()=>{expect(result.current.actions.undo()).toBe(true)})
 const undone=queued.splice(0);act(()=>undone.forEach(deliver))
 expect(Array.from(result.current.entities)).toEqual(before);expect(sameBytes(inner.posted.at(-1).bytes,second)).toBe(true)
},scaleTimeout(90_000))

for(const eventType of ['error','messageerror'])it(eventType+' clears pending state and recovers fresh',async()=>{
 const initial=realWorkerTransport(),recovered=realWorkerTransport(),events=new Map(),queue=[];let hold=false
 const listeners=new Set();initial.addEventListener('message',e=>{if(hold)queue.push(e);else listeners.forEach(f=>f(e))})
 const wrapped={postMessage:m=>initial.postMessage(m),terminate:()=>initial.terminate(),addEventListener:(t,f)=>{if(t==='message')listeners.add(f);else events.set(t,f)},removeEventListener:(t,f)=>listeners.delete(f)}
 let spawned=0;const {result}=renderHook(()=>useEngineSession({createWorker:()=>spawned++===0?wrapped:recovered}))
 await act(async()=>{result.current.actions.openBytes(original,'crashing.dxf',{committed:true})})
 const chosen=result.current.entities.find(r=>r.type==='LWPOLYLINE'&&r.editable!==false);act(()=>result.current.actions.select(chosen.id));hold=true
 await act(async()=>{result.current.actions.applyEdit('move',{dx:'1',dy:'0'})})
 expect(result.current.busy).toBe(true);expect(queue.length).toBeGreaterThan(0)
 act(()=>events.get(eventType)({type:eventType}))
 expect(result.current.busy).toBe(false);expect(result.current.recoverable).toBe(true);expect(result.current.undoDepth).toBe(0);expect(result.current.entities).toHaveLength(0)
 const second=fixture(1000)
 await act(async()=>{result.current.actions.openBytes(second,'recovered.dxf',{committed:true})})
 const before=clone(Array.from(result.current.entities));expect(before).toHaveLength(2);expect(result.current.errorKind).toBeNull();expect(result.current.documentId).toBe('recovered.dxf')
 act(()=>queue.forEach(e=>listeners.forEach(f=>f(e))))
 expect(Array.from(result.current.entities)).toEqual(before);expect(result.current.undoDepth).toBe(0)
 const target=before.find(r=>r.type==='LWPOLYLINE'&&r.editable!==false);act(()=>result.current.actions.select(target.id))
 await act(async()=>{result.current.actions.applyEdit('move',{dx:'2',dy:'0'})});expect(result.current.errorKind).toBeNull();expect(result.current.undoDepth).toBe(1)
 await act(async()=>{expect(result.current.actions.undo()).toBe(true)})
 expect(Array.from(result.current.entities)).toEqual(before);expect(sameBytes(recovered.posted.at(-1).bytes,second)).toBe(true)
},scaleTimeout(90_000))
})
