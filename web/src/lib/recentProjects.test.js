import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  RECENT_PROJECTS_KEY, addRecentProject, readProjectPrincipal, readRecentProjects,
  recentProjectsKey, togglePinnedProject, writeRecentProjects,
} from './recentProjects.js'

afterEach(() => vi.unstubAllGlobals())

function memoryStorage() {
  const values = new Map()
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), values }
}

describe('recent project preferences', () => {
  it('keeps the last five unique opened ids, newest first', () => {
    let state = readRecentProjects('alice', null)
    for (const id of ['p1', 'p2', 'p3', 'p4', 'p5', 'p6', 'p3']) state = addRecentProject(state, id)
    expect(state.recent).toEqual(['p3', 'p6', 'p5', 'p4', 'p2'])
    const storage = memoryStorage()
    writeRecentProjects('alice', state, storage)
    expect(readRecentProjects('alice', storage)).toEqual(state)
  })

  it('isolates both recents and pins by principal, with no anonymous key', () => {
    const storage = memoryStorage()
    const state = togglePinnedProject(addRecentProject(null, 'p1'), 'p2')
    writeRecentProjects('alice', state, storage)
    writeRecentProjects('bob', addRecentProject(null, 'p3'), storage)
    expect(recentProjectsKey('alice')).toContain(RECENT_PROJECTS_KEY)
    expect(recentProjectsKey('alice')).not.toBe(recentProjectsKey('bob'))
    expect(readRecentProjects('alice', storage)).toEqual({ recent: ['p1'], pinned: ['p2'] })
    expect(readRecentProjects('bob', storage)).toEqual({ recent: ['p3'], pinned: [] })
    expect(writeRecentProjects(null, state, storage)).toBe(false)
    expect(readRecentProjects(null, storage)).toEqual({ recent: [], pinned: [] })
    expect(storage.values.size).toBe(2)
  })

  it('persists ids only, discarding project objects, names, duplicates and extra fields', () => {
    const storage = memoryStorage()
    const value = { recent: ['p1', { id: 'p2', name: 'Private' }, '', null, 'p1', 'p3'],
      pinned: ['p3', { project_id: 'p4' }, 'p3'], name: 'Private', token: 'secret' }
    writeRecentProjects('alice', value, storage)
    expect(storage.getItem(recentProjectsKey('alice'))).toBe(JSON.stringify({ recent: ['p1', 'p3'], pinned: ['p3'] }))
    storage.setItem(recentProjectsKey('alice'), JSON.stringify(value))
    expect(readRecentProjects('alice', storage)).toEqual({ recent: ['p1', 'p3'], pinned: ['p3'] })
    expect(addRecentProject(null, { id: 'p1' }).recent).toEqual([])
  })

  it('toggles pins independently of recent history', () => {
    const state = addRecentProject(null, 'p1')
    const pinned = togglePinnedProject(state, 'p2')
    expect(pinned).toEqual({ recent: ['p1'], pinned: ['p2'] })
    expect(togglePinnedProject(pinned, 'p2')).toEqual(state)
    expect(state.pinned).toEqual([])
  })

  it('degrades safely for corrupt data and throwing storage methods or accessors', () => {
    const storage = memoryStorage()
    storage.setItem(recentProjectsKey('alice'), '{broken')
    expect(readRecentProjects('alice', storage)).toEqual({ recent: [], pinned: [] })
    const throwing = { getItem: () => { throw new Error('locked') }, setItem: () => { throw new Error('full') } }
    expect(readRecentProjects('alice', throwing)).toEqual({ recent: [], pinned: [] })
    expect(writeRecentProjects('alice', { recent: ['p1'] }, throwing)).toBe(false)
    vi.stubGlobal('localStorage', throwing)
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, get() { throw new Error('locked') } })
    expect(readRecentProjects('alice')).toEqual({ recent: [], pinned: [] })
    expect(writeRecentProjects('alice', { pinned: ['p1'] })).toBe(false)
    expect(readProjectPrincipal()).toBeNull()
  })

  it('uses identity claims, surviving token refresh and rejecting opaque bearers', () => {
    const storage = memoryStorage()
    const token = (claims, signature) => `header.${btoa(JSON.stringify(claims))}.${signature}`
    storage.setItem('leaf.jwt', token({ iss: 'issuer', sub: 'alice' }, 'first'))
    const principal = readProjectPrincipal(storage)
    storage.setItem('leaf.jwt', token({ iss: 'issuer', sub: 'alice' }, 'refreshed'))
    expect(readProjectPrincipal(storage)).toBe(principal)
    storage.setItem('leaf.jwt', token({ iss: 'issuer', sub: 'bob' }, 'first'))
    expect(readProjectPrincipal(storage)).not.toBe(principal)
    storage.setItem('leaf.jwt', 'opaque-secret')
    expect(readProjectPrincipal(storage)).toBeNull()
    storage.setItem('leaf.jwt', token({}, 'first'))
    expect(readProjectPrincipal(storage)).toBeNull()
  })
})
