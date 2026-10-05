import { test, expect, runProbe, setupStep } from './fixtures.mjs'
import { buildFeatureMap } from '../../walk/featureMap.mjs'
import { resolveProbe } from './probes.mjs'

const entries = buildFeatureMap().entries

async function reachable(locator) {
  await expect(locator).toBeVisible()
  await locator.scrollIntoViewIfNeeded()
  const measurement = await locator.evaluate(element => {
    const box = element.getBoundingClientRect()
    return {
      label: element.innerText || element.getAttribute('aria-label'),
      x: box.x, y: box.y, width: box.width, height: box.height,
      hits: [0.2, 0.5, 0.8].map(x => {
        const hit = document.elementFromPoint(box.x + box.width * x, box.y + box.height / 2)
        return hit === element || element.contains(hit)
      }),
    }
  })
  expect(measurement.width, measurement.label).toBeGreaterThan(0)
  expect(measurement.height, measurement.label).toBeGreaterThan(0)
  expect(measurement.hits, measurement.label).toEqual([true, true, true])
  return measurement
}

async function snapshot(page, testInfo, name, measurements) {
  await testInfo.attach(name + '-geometry', {
    body: Buffer.from(JSON.stringify(measurements, null, 2)), contentType: 'application/json',
  })
  await testInfo.attach(name, { body: await page.screenshot({ fullPage: true }), contentType: 'image/png' })
}

async function member(featureId, state, runtime) {
  const entry = entries.find(entry => entry.id === featureId)
  expect(entry, featureId).toBeTruthy()
  const probe = resolveProbe(entry, state)
  // The existing runner uses ordinary locator clicks for setup, activation
  // and cleanup, and asserts the registry's actual effect for each member.
  const result = await runProbe(probe, runtime)
  expect(result?.unsupported, result?.reason).not.toBe(true)
  expect(runtime.evidence.setupCompleted).toBeTruthy()
  expect(runtime.evidence.oracleReached).toBe(probe.assertion.assertionId)
  expect(runtime.evidence.cleanupCompleted).toBe(true)
}

async function phoneActionHitTarget(featureId, state, runtime, measurements) {
  const entry = entries.find(entry => entry.id === featureId)
  expect(entry, featureId).toBeTruthy()
  const probe = resolveProbe(entry, state)
  runtime.cleanup = []
  // Share the ready drawing setup, then establish pane-open with the reachable toggle.
  const setupProbe = featureId === 'action:properties-pane' && state === 'pane-open'
    ? resolveProbe(entry, 'ready') : probe
  for (const recipe of setupProbe.setup.steps) {
    if (recipe.kind === 'open-private-drawing' || recipe.kind === 'ribbon-tab') {
      await setupStep(setupProbe, runtime, recipe)
    }
  }
  if (featureId === 'action:rail-expand') {
    const manage = runtime.page.getByRole('tablist', { name: 'Ribbon', exact: true })
      .getByRole('tab', { name: 'Manage', exact: true })
    measurements.push(await reachable(manage))
    await manage.click()
    await expect(manage).toHaveAttribute('aria-selected', 'true')
  }
  const toolbar = runtime.page.getByRole('toolbar', { name: 'Drafting tools', exact: true })
  const scope = featureId === 'action:rail-expand'
    ? toolbar.getByRole('group', { name: 'Rail', exact: true }) : toolbar
  const target = scope.getByRole(probe.locator.role, { name: probe.locator.name, exact: true })
  if (!await target.isVisible()) {
    const more = toolbar.getByRole('button', { name: 'More panels', exact: true })
    await expect(more).toBeVisible()
    if (await more.getAttribute('aria-expanded') === 'false') await more.click()
  }
  measurements.push(await reachable(target))
  if (featureId === 'action:properties-pane' && state === 'pane-open') {
    const pane = runtime.page.getByRole('complementary', { name: 'Properties', exact: true })
    if (!await pane.isVisible()) await target.click()
    await expect(pane).toBeVisible()
    measurements.push(await reachable(target))
  }
  // A normal Playwright click must pass its native pointer interception check.
  await target.click()

  let result
  let failureMessage = null
  try {
    result = await runProbe(probe, runtime)
  } catch (error) {
    failureMessage = error.message
  }
  await runtime.testInfo.attach('downstream-oracle', {
    body: Buffer.from(JSON.stringify({
      featureId, state, oracleReached: runtime.evidence.oracleReached ?? null,
      failureMessage: failureMessage ?? runtime.evidence.failure?.message ?? result?.reason ?? null,
      unsupported: result?.unsupported === true,
    }, null, 2)), contentType: 'application/json',
  })
}

if (process.env.LEAF_WALK_PROOF === '1') {
  for (const [featureId, state] of [
    // properties-pane [pane-open] and rail-expand [ready] at phone are wave B
    // map/product questions (no Properties pane and no Rail group on phone).
    ['action:properties-pane', 'ready'],
    ['drawer:nav', 'closed'],
    ['drawer:nav', 'open'],
  ]) {
    test(`u5-shell-hit-targets ${featureId} [${state}] 390x844 @phone`,
      async ({ page, stack, walkEvidence }, testInfo) => {
        await page.setViewportSize({ width: 390, height: 844 })
        const measurements = []
        try {
          const runtime = { page, stack, evidence: walkEvidence, testInfo }
          if (featureId === 'action:properties-pane' || featureId === 'action:rail-expand') {
            await phoneActionHitTarget(featureId, state, runtime, measurements)
          } else {
            await member(featureId, state, runtime)
          }
          const ribbon = page.getByRole('tablist', { name: 'Ribbon', exact: true })
          for (const name of ['View', 'Manage']) {
            const tab = ribbon.getByRole('tab', { name, exact: true })
            measurements.push(await reachable(tab))
            await tab.click()
            await expect(tab).toHaveAttribute('aria-selected', 'true')
          }
          const rail = page.getByRole('button', { name: 'Tool rail', exact: true })
          measurements.push(await reachable(rail))
          const expanded = await rail.getAttribute('aria-expanded')
          await rail.click()
          await expect(rail).toHaveAttribute('aria-expanded', expanded === 'true' ? 'false' : 'true')
          await rail.click()
          await expect(rail).toHaveAttribute('aria-expanded', expanded)
        } finally {
          await snapshot(page, testInfo, 'phone-header', { featureId, state, measurements })
        }
      })
  }

  for (const viewport of [{ width: 1600, height: 1000 }, { width: 1280, height: 800 }]) {
    for (const state of ['failed-load', 'fullscreen', 'windowed']) {
      test(`u5-shell-hit-targets control:fullscreen [${state}] ${viewport.width}x${viewport.height} @desktop`,
        async ({ page, stack, walkEvidence }, testInfo) => {
          await page.setViewportSize(viewport)
          const measurements = []
          try {
            await member('control:fullscreen', state, { page, stack, evidence: walkEvidence, testInfo })
            const toolbar = page.locator('footer.foot-bar').getByRole('toolbar', { name: 'Drafting settings', exact: true })
            const fullscreen = toolbar.getByRole('button', { name: 'Toggle fullscreen', exact: true })
            await expect(page.getByRole('button', { name: /^Expand the job monitor \([0-9]+ live\)$/ })).toBeVisible()
            measurements.push({ posture: 'windowed', toolbar: await reachable(toolbar), fullscreen: await reachable(fullscreen) })
            await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false)
            await fullscreen.click()
            await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(true)
            measurements.push({ posture: 'fullscreen', toolbar: await reachable(toolbar), fullscreen: await reachable(fullscreen) })
            await snapshot(page, testInfo, 'desktop-fullscreen', { viewport, state, measurements })
            await fullscreen.click()
            await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false)
            measurements.push({ posture: 'restored-windowed', toolbar: await reachable(toolbar), fullscreen: await reachable(fullscreen) })
          } finally {
            if (!page.isClosed() && await page.evaluate(() => !!document.fullscreenElement)) {
              await page.getByRole('button', { name: 'Toggle fullscreen', exact: true }).click()
              await expect.poll(() => page.evaluate(() => !!document.fullscreenElement)).toBe(false)
            }
            await snapshot(page, testInfo, 'desktop-windowed', { viewport, state, measurements })
          }
        })
    }
  }
}
