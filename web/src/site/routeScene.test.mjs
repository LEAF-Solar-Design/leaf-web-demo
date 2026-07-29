import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { sceneForPath } from './routeScene.js'

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
