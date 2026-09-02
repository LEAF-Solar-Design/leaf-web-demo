import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { describe, it } from 'node:test'

import { activeCastForScene, sceneAllowsMarketingEject, sceneForPath } from './routeScene.js'

describe('site route scenes', () => {
  it('renders /ty through the same application scene as /app', () => {
    assert.equal(sceneForPath('/app'), 'app')
    assert.equal(sceneForPath('/ty'), sceneForPath('/app'))
    assert.equal(sceneForPath('/ty/'), sceneForPath('/app/'))
    assert.equal(sceneForPath('/ty/project'), sceneForPath('/app/project'))
  })

  it('preserves the existing /app and /try routing', () => {
    assert.equal(sceneForPath('/app/project'), 'app')
    assert.equal(sceneForPath('/try'), 'tool')
    assert.equal(sceneForPath('/'), 'site')
  })
})

// Named convergence bugs (a)/(b), fixed ahead of the W3 one-shell mount.
// ACCEPTANCE route matrix: Esc must never navigate('/') in console mode.
describe('marketing eject predicate', () => {
  it('permits the Esc eject only from the operator stage', () => {
    assert.equal(sceneAllowsMarketingEject('tool'), true)
    assert.equal(sceneAllowsMarketingEject('app'), false)
    assert.equal(sceneAllowsMarketingEject('site'), false)
    assert.equal(sceneAllowsMarketingEject('sheets'), false)
    assert.equal(sceneAllowsMarketingEject(undefined), false)
  })
})

describe('inert sweep cast', () => {
  it('names a cast only for stage scenes; null means do not sweep', () => {
    assert.equal(activeCastForScene('tool'), 'tool')
    assert.equal(activeCastForScene('site'), 'site')
    assert.equal(activeCastForScene('app'), null)
    assert.equal(activeCastForScene('sheets'), null)
  })
})

// Source pins: SiteRoot must ROUTE THROUGH the predicates, or the pure
// functions above pin nothing. Normalized to LF (Windows checkout is CRLF).
describe('SiteRoot wiring', () => {
  const src = readFileSync(new URL('./SiteRoot.jsx', import.meta.url), 'utf8').replace(/\r\n/g, '\n')

  it('gates the Escape eject on sceneAllowsMarketingEject, not a scene literal', () => {
    assert.match(src, /e\.key === 'Escape' && sceneAllowsMarketingEject\(scene\)/)
    assert.doesNotMatch(src, /e\.key === 'Escape' && scene === /)
  })

  it('derives the inert sweep cast from activeCastForScene and skips on null', () => {
    assert.match(src, /const activeCast = activeCastForScene\(scene\)/)
    assert.match(src, /if \(activeCast === null\) return/)
  })
})
