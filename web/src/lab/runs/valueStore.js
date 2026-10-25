import { useCallback, useSyncExternalStore } from 'react'

export const terminalState = state => ['finished', 'failed', 'cancelled', 'missing'].includes(state)
const notify = listeners => listeners?.forEach(listener => listener())
const listen = (map, key, listener) => {
  if (!map.has(key)) map.set(key, new Set())
  map.get(key).add(listener)
  return () => { const set = map.get(key); set?.delete(listener); if (!set?.size) map.delete(key) }
}

export function createValueStore({ maxEvents = 10000, maxRuns = 20 } = {}) {
  const runs = new Map(), runListeners = new Map(), blockListeners = new Map(), listeners = new Set()
  const streamControls = new Map()
  const recovering = new Set()
  const nextState = (run, state) => terminalState(run.state) && !terminalState(state) ? run.state : state
  const clearTerminalControl = id => {
    const run = runs.get(id)
    if (terminalState(run?.state) && !recovering.has(id) && run.transport.status !== 'auth_expired') streamControls.delete(id)
  }
  let list = [], events = [], dropped = 0, order = 0
  maxEvents = Math.max(0, Math.floor(maxEvents))
  maxRuns = Math.min(20, Math.max(1, Math.floor(maxRuns)))
  const publishList = () => { list = [...runs.values()].sort((a, b) => b.order - a.order); notify(listeners) }
  const publish = id => { notify(runListeners.get(id)); publishList() }
  function trim() {
    while ([...runs.keys()].filter(id => id !== 'local').length > maxRuns) {
      const oldest = [...runs.values()].filter(run => run.id !== 'local' && terminalState(run.state) && !recovering.has(run.id) && run.transport.status !== 'auth_expired').sort((a, b) => a.order - b.order)[0]
      if (!oldest) break // Active runs are never evicted, even when capacity is exhausted.
      runs.delete(oldest.id)
      streamControls.delete(oldest.id)
      events = events.filter(event => event.runId !== oldest.id)
      notify(runListeners.get(oldest.id))
      Object.keys(oldest.blocks).forEach(block => notify(blockListeners.get(`${oldest.id}/${block}`)))
    }
  }
  function ensure(id, metadata = {}) {
    if (!runs.has(id) && id !== 'local' && [...runs.keys()].filter(key => key !== 'local').length >= maxRuns) {
      const oldest = [...runs.values()].filter(run => run.id !== 'local' && terminalState(run.state) && !recovering.has(run.id) && run.transport.status !== 'auth_expired').sort((a, b) => a.order - b.order)[0]
      if (!oldest) throw new Error('Too many active runs')
      if (oldest) {
        runs.delete(oldest.id)
        streamControls.delete(oldest.id)
        events = events.filter(event => event.runId !== oldest.id)
        notify(runListeners.get(oldest.id))
        Object.keys(oldest.blocks).forEach(block => notify(blockListeners.get(`${oldest.id}/${block}`)))
      }
    }
    if (!runs.has(id)) runs.set(id, { id, state: 'queued', graph_revision: null, lastSeq: -1,
      blocks: {}, events: [], dropped: 0, gaps: [], transport: { status: 'idle', lagMs: null, lastEventAt: null }, order: ++order, ...metadata })
    return runs.get(id)
  }
  const store = {
    getRun: id => runs.get(id), getBlock: (id, block) => runs.get(id)?.blocks[block],
    getRuns: () => list, getEvents: () => events, get dropped() { return dropped },
    canAddRun: () => [...runs.keys()].filter(id => id !== 'local').length < maxRuns
      || [...runs.values()].some(run => run.id !== 'local' && terminalState(run.state) && !recovering.has(run.id) && run.transport.status !== 'auth_expired'),
    subscribeBlock: (id, block, listener) => listen(blockListeners, `${id}/${block}`, listener),
    subscribeRun: (id, listener) => listen(runListeners, id, listener),
    subscribeRuns(listener) { listeners.add(listener); return () => listeners.delete(listener) },
    setStreamControl(id, control) {
      if (runs.has(id) && (!terminalState(runs.get(id).state) || recovering.has(id) || runs.get(id).transport.status === 'auth_expired')) streamControls.set(id, control)
    },
    clearStreamControl(id) { streamControls.delete(id) },
    resume(id) {
      const control = streamControls.get(id)
      if (runs.get(id)?.transport.status !== 'auth_expired' || !control) return false
      control.retry()
      return true
    },
    upsertRun(id, metadata = {}) {
      const run = ensure(id)
      runs.set(id, { ...run, ...metadata, state: nextState(run, metadata.state ?? run.state), id })
      clearTerminalControl(id)
      trim(); publish(id)
      return runs.get(id)
    },
    setTransport(id, patch) {
      const run = ensure(id)
      runs.set(id, { ...run, transport: { ...run.transport, ...patch } }); publish(id)
      clearTerminalControl(id)
    },
    addGap(id, gap) {
      const run = ensure(id)
      const gaps = [...run.gaps]
      if (gap.recovered && gaps.length && !gaps.at(-1).recovered) gaps[gaps.length - 1] = { ...gaps.at(-1), ...gap }
      else gaps.push(gap)
      runs.set(id, { ...run, gaps: gaps.slice(-20) }); publish(id)
    },
    // Reset the cursor and values before replaying a snapshot. Old retained
    // events are removed so recovery does not duplicate the visible log.
    resetSnapshot(id, metadata) {
      const run = ensure(id)
      recovering.add(id)
      events = events.filter(event => event.runId !== id)
      runs.set(id, { ...run, ...metadata, state: nextState(run, metadata.state ?? run.state), id, lastSeq: -1, blocks: {}, events: [] })
      Object.keys(run.blocks).forEach(block => notify(blockListeners.get(`${id}/${block}`)))
      publish(id)
    },
    finishSnapshot(id) {
      recovering.delete(id)
      clearTerminalControl(id)
      trim(); publish(id)
    },
    apply(id, event) {
      const run = ensure(id)
      if (!Number.isInteger(event.seq) || event.seq <= run.lastSeq) return false
      const block = event.block_id
      const blocks = block ? { ...run.blocks, [block]: { value: event.kind === 'value' ? event.payload : run.blocks[block]?.value,
        seq: event.seq, at: event.at, error: ['failed', 'suspended'].includes(event.kind) ? event.payload?.reason || event.kind : null } } : run.blocks
      const retained = { ...event, runId: id }
      events.push(retained)
      runs.set(id, { ...run, blocks, lastSeq: event.seq, events: [...run.events, retained],
        state: ['finished', 'failed', 'cancelled'].includes(event.kind) && !block ? event.kind : run.state === 'queued' ? 'running' : run.state,
        transport: { ...run.transport, lastEventAt: event.at } })
      clearTerminalControl(id)
      while (events.length > maxEvents) {
        const removed = events.shift(); dropped += 1
        const owner = runs.get(removed.runId)
        if (owner) runs.set(owner.id, { ...owner, events: owner.events.filter(item => item !== removed), dropped: owner.dropped + 1 })
        if (removed.runId !== id) notify(runListeners.get(removed.runId))
      }
      if (block) notify(blockListeners.get(`${id}/${block}`))
      trim(); publish(id)
      return true
    },
    setLocal(values, timing) {
      const previous = ensure('local', { state: 'running' })
      const blocks = {}
      for (const value of values) {
        const entry = blocks[value.id] || { value: {}, seq: previous.lastSeq + 1, at: null, error: null }
        entry.value[value.port] = value
        blocks[value.id] = entry
      }
      runs.set('local', { ...previous, lastSeq: previous.lastSeq + 1, blocks, timing })
      new Set([...Object.keys(previous.blocks), ...Object.keys(blocks)]).forEach(block => notify(blockListeners.get(`local/${block}`)))
      publish('local')
    },
  }
  return store
}

export const labValueStore = createValueStore()
export function useBlockValue(store, runId, blockId) {
  return useSyncExternalStore(useCallback(listener => store.subscribeBlock(runId, blockId, listener), [store, runId, blockId]),
    useCallback(() => store.getBlock(runId, blockId), [store, runId, blockId]))
}
export function useRun(store, runId) {
  return useSyncExternalStore(useCallback(listener => store.subscribeRun(runId, listener), [store, runId]),
    useCallback(() => store.getRun(runId), [store, runId]))
}
export function useRunStatus(store, runId) {
  return useSyncExternalStore(useCallback(listener => store.subscribeRun(runId, listener), [store, runId]),
    useCallback(() => {
      const run = store.getRun(runId)
      return run ? `${run.state}/${run.transport.status}` : ''
    }, [store, runId]))
}
export function useRuns(store = labValueStore) {
  return useSyncExternalStore(store.subscribeRuns, store.getRuns)
}
