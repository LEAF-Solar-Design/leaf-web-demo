import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test } from 'vitest'

test('re-exports ProjectBoardGround from ProjectBoardGround.jsx by identity', async () => {
  const shim = await import('./SurfaceGrounds.jsx')
  const ground = await import('./ProjectBoardGround.jsx')
  expect(shim.ProjectBoardGround).toBe(ground.ProjectBoardGround)
})

test('re-exports DeviceGround from ios/DeviceGround.jsx by identity', async () => {
  const shim = await import('./SurfaceGrounds.jsx')
  const ground = await import('../ios/DeviceGround.jsx')
  expect(shim.DeviceGround).toBe(ground.DeviceGround)
})

test('mounts the native Ops ground once in the shared ground switch', () => {
  const source = readFileSync(resolve(process.cwd(), 'src/site/SurfaceGrounds.jsx'), 'utf8')
  expect(source.match(/<OpsWorkspaceGround\b/g)).toHaveLength(1)
  expect(source).toContain("surfaceGround(surface) === 'integrations'")
  const app = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')
  const portal = app.slice(app.indexOf('<SurfaceGrounds'))
  for (const prop of ['projectId={openProjectId || null}', 'session={session}', 'signedIn={signedIn}', 'accountScope={projectSolarAccountScope}']) expect(portal).toContain(prop)
})

test('re-exports measureGroundWindow and measureContainedWindow from groundWindow.js by identity', async () => {
  const shim = await import('./SurfaceGrounds.jsx')
  const windowMath = await import('./groundWindow.js')
  expect(shim.measureGroundWindow).toBe(windowMath.measureGroundWindow)
  expect(shim.measureContainedWindow).toBe(windowMath.measureContainedWindow)
})

test('the shim defines neither ground component nor the window math itself', () => {
  const source = readFileSync(resolve(process.cwd(), 'src/site/SurfaceGrounds.jsx'), 'utf8')
  for (const definition of [
    /function\s+ProjectBoardGround\b/,
    /function\s+DeviceGround\b/,
    /function\s+BoardTiles\b/,
    /function\s+measureGroundWindow\b/,
    /function\s+measureContainedWindow\b/,
  ]) {
    expect(source).not.toMatch(definition)
  }
})

test('ProjectBoardGround renders its tiles through BoardTiles.jsx', async () => {
  const source = readFileSync(resolve(process.cwd(), 'src/site/ProjectBoardGround.jsx'), 'utf8')
  expect(source).toContain("from './BoardTiles.jsx'")
  expect(source).toContain('<BoardTiles')
  expect((await import('./BoardTiles.jsx')).BoardTiles).toBeTypeOf('function')
})
